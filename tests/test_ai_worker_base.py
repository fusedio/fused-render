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
import re
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


def test_a_cancelled_download_is_not_reported_as_a_failed_load(base, monkeypatch):
    """A ✕ pressed during the fetch is not a crash, and calling it one is not
    merely a wrong word.

    `fetch_with_progress` learns about the ✕ from the reply to its own tick and
    raises `Cancelled` — which the broad catch below turned into a terminal
    `state="error"` on the row. That state CLEARS `cancel_requested`
    (`jobs.upsert`), so the supervisor's own poll — the thing that would have
    written "cancelled" half a second later — could no longer see the ✕ at all,
    read /health, found "error", and reported the download the user stopped as a
    load that failed.
    """
    reports = []
    monkeypatch.setattr(base, "report",
                        lambda job=None, **fields: reports.append(fields) or None)

    def download(model_id):
        raise base.Cancelled()

    base._bring_up("org/m", download, lambda model_id, fetched: None)

    assert [r["state"] for r in reports if "state" in r] == ["cancelled"]
    # The health error is the literal string the supervisor switches on, so its
    # independent verdict agrees with the row rather than overwriting it.
    assert base.snapshot()["error"] == "cancelled"


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


# -- a fetch that happens inside a REQUEST, on a row with a live ✕ ---------------
#
# These fetches used to own a model-load row, whose ✕ the supervisor answers by
# killing the process — so a plain `report` was enough. A component model pulled
# during a transcription reports into a row whose `cancellable` is True and
# whose ✕ has to stop THIS work, and the reply to the tick is the only channel
# that reaches a thread parked inside huggingface_hub.


def _slow_fetch(seconds=0.2):
    def call():
        time.sleep(seconds)
        return "/snap"
    return call


def test_a_fetch_honours_the_cross_pressed_on_the_row_it_reports_to(base, monkeypatch):
    """`cancel_requested` comes back on the reply to the tick we were sending
    anyway. Without this the user pressed ✕, the manager recorded it, and 33MB
    carried on downloading behind a row that went on saying "running"."""
    monkeypatch.setattr(base, "repo_folder", lambda model_id, repo_type="model": "/repo")
    monkeypatch.setattr(base, "bytes_on_disk", lambda folder: 512)
    # `report` rather than `_send`: `report` short-circuits to None unless
    # `JOB_URL` is a real http address, so a `_send` stub is never reached here
    # and the test would pass against a fetch that ignores cancellation.
    monkeypatch.setattr(base, "report",
                        lambda job=None, **fields: {"cancel_requested": True})

    with pytest.raises(base.Cancelled):
        base.fetch_with_progress("org/m", _slow_fetch(), total=1024,
                                 job="sys:ai-transcribe:x")


def test_a_fetch_honours_the_cancel_ROUTE_when_it_is_inside_a_request(base, monkeypatch):
    """The supervisor POSTs `/cancel` as well as setting the row's flag, and a
    fetch that read only one of the two channels would honour a ✕ or not
    depending on which arrived first."""
    monkeypatch.setattr(base, "repo_folder", lambda model_id, repo_type="model": "/repo")
    monkeypatch.setattr(base, "bytes_on_disk", lambda folder: 512)
    monkeypatch.setattr(base, "report", lambda job=None, **fields: None)
    base.CANCEL.set()
    try:
        with pytest.raises(base.Cancelled):
            base.fetch_with_progress("org/m", _slow_fetch(), total=1024,
                                     job="sys:ai-transcribe:x")
    finally:
        base.CANCEL.clear()


def test_a_model_DOWNLOAD_ignores_a_cancel_flag_left_by_an_earlier_generation(
        base, monkeypatch):
    """`CANCEL` belongs to whatever holds `GENERATE_LOCK` and is cleared by
    `_single`/`_stream` on the way in — so it means THIS fetch exactly when this
    fetch is inside a request. `_bring_up` runs on its own thread with no such
    lock, and reading the flag there would abort a download nobody asked to
    stop because some earlier generation was cancelled. Hence the `job` gate:
    no job, no route-flag reading. The row's own ✕ still works, via the reply."""
    monkeypatch.setattr(base, "repo_folder", lambda model_id, repo_type="model": "/repo")
    monkeypatch.setattr(base, "bytes_on_disk", lambda folder: 512)
    monkeypatch.setattr(base, "report", lambda job=None, **fields: None)
    base.CANCEL.set()
    try:
        assert base.fetch_with_progress("org/m", _slow_fetch(), total=1024) == "/snap"
    finally:
        base.CANCEL.clear()


def test_a_cross_that_lands_as_the_fetch_FINISHES_keeps_the_bytes(base, monkeypatch):
    """The same rule `_call_with_ticks` states about a finished decode: the
    bytes are on the disk, and raising here would make the next attempt
    re-download what this one already has. The final report is a plain
    `report`, and the loop does not tick for a thread that has already ended."""
    monkeypatch.setattr(base, "repo_folder", lambda model_id, repo_type="model": "/repo")
    monkeypatch.setattr(base, "bytes_on_disk", lambda folder: 1024)
    # Returns immediately, so the ✕ can only be seen by the first report (before
    # any bytes moved) or the last (after they all did). The first is honoured;
    # the last is not, and this pins that the completed value comes back.
    calls = {"n": 0}

    def reporting(job=None, **fields):
        calls["n"] += 1
        # Not cancelled until the fetch is done — the late-cancel window.
        return {"cancel_requested": calls["n"] > 1}

    monkeypatch.setattr(base, "report", reporting)
    assert base.fetch_with_progress("org/m", lambda: "/snap", total=1024,
                                    job="sys:ai-transcribe:x") == "/snap"


def test_every_fetch_tick_can_rebuild_the_row_it_reports_to(base, monkeypatch):
    """A component fetch lands on a row the manager can evict at any tick, and
    a report with no `title` is refused outright — which kills the row for good.
    The identity has to ride on EVERY tick, not just the first."""
    ticks = []
    monkeypatch.setattr(base, "repo_folder", lambda model_id, repo_type="model": "/repo")
    monkeypatch.setattr(base, "bytes_on_disk", lambda folder: 512)
    monkeypatch.setattr(base, "report",
                        lambda job=None, **fields: ticks.append(fields) or None)
    row = {"title": "meeting.m4a", "kind": "task", "cancellable": True, "unit": "s"}

    base.fetch_with_progress("org/m", _slow_fetch(1.2), total=1024,
                             job="sys:ai-transcribe:x", row=row)

    assert len(ticks) >= 3, ticks
    for tick in ticks:
        assert tick["title"] == "meeting.m4a", tick
        assert tick["cancellable"] is True, tick
        assert tick["state"] == "running", tick
        # …and for the duration of a download the row IS one: `kind`/`unit` are
        # this function's own and override the row's, so the manager draws
        # bytes rather than a seconds clock over a byte count.
        assert tick["kind"] == "download" and tick["unit"] == "bytes", tick


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


def _runner_source(runner):
    """The module that actually IMPLEMENTS a runner, following a shell.

    Six of the eleven runner folders — the CPU/CUDA/ROCm variants of the two
    torch engines — hold a five-line `worker.py` that inserts `runners/` on the
    path and calls `torch_text.main()` or `torch_image.main()`. The behaviour
    lives one level up, so a source assertion made against the folder's file
    would read a shell and pass on anything: the two tests below would have
    stopped checking the torch runners entirely, silently, on the commit that
    hoisted them.

    The shell is recognised by what makes it one — it imports a module from the
    runners root and CALLS ITS `main()`, which is the whole of its body — rather
    than by a list of runner codes, so a future hoisted runner is covered without
    an edit here. Recognising it by the import alone would follow every worker's
    `import worker_base` into the base module instead.
    """
    with open(runner.worker, encoding="utf-8") as handle:
        source = handle.read()
    root = os.path.dirname(runner.folder)
    for match in re.finditer(r"^import (\w+)", source, re.MULTILINE):
        name = match.group(1)
        hoisted = os.path.join(root, name + ".py")
        if f"{name}.main()" in source and os.path.isfile(hoisted):
            with open(hoisted, encoding="utf-8") as handle:
                return handle.read()
    return source


def test_no_runner_reimplements_the_contract():
    """The whole point of the extraction (AI-9a).

    A worker that grew its own HTTP server, its own auth check or its own
    reporter would put the SUPERVISOR's contract back in two places — the exact
    drift this module exists to prevent, and invisible until the two disagree.
    Checked as source, because the alternative is running mlx on Linux.
    """
    from fused_render.ai import registry

    for runner in registry.all_runners():
        source = _runner_source(runner)
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
        source = _runner_source(runner)
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


# The image recipe's own patterns moved to tests/test_ai_diffusers_worker.py,
# where they are asserted against a frozen listing of the real repo instead of
# by grepping the source for a pattern string. That grep was the reason a recipe
# whose deny-list saved nothing still passed: the string it looked for was
# present and the 7.75GB root bundle beside it was not something a substring
# check could see.

# -- the cached path does not touch the network ---------------------------------
#
# Measured on this machine for `mlx-community/whisper-tiny.en-8bit`, fully
# cached: `worker_base.download_snapshot` cost 483ms and `download_file` 456ms,
# every millisecond of it Hub round-trips — `HfApi().model_info(files_metadata=
# True)` is 228ms on its own and the segmented fetch's own requests are another
# ~230ms. The same answers off the cache alone are 0.13ms and 0.14ms. That is why
# a bring-up of a model already on disk waited about a second before the ~14ms of
# actual weight loading, and it is what produced the "You are sending
# unauthenticated requests to the HF Hub" line in every worker log.
#
# `tests/test_ai_hub_fetch.py` owns the other side of this: that pressing Stop
# never starts a download, enforced by counting calls to hf's two download
# functions. That test is why the fast path looks at the FILESYSTEM first and
# only then asks hf to confirm — and it caught this file getting it wrong.


class _LocalHub:
    """`huggingface_hub`, recording which calls were LOCAL and which networked.

    The distinction is the whole subject: a cached model must be resolved off the
    disk with `local_files_only=True` and nothing else, an absent one must still
    take exactly the path it took before, and a repo the cache has never held
    must not reach a download function at all.
    """

    class LocalEntryNotFoundError(OSError):
        """hf's own name for "the cache cannot answer this", and hf's own base:
        `LocalEntryNotFoundError` really is a `FileNotFoundError`, which is what
        lets `_NOT_CACHED` name it without importing it."""

    def __init__(self, cached=(), snapshot="/cache/snap", file_path=None):
        self.cached = set(cached)
        self.snapshot = snapshot
        self.file_path = file_path
        #: (function, local_files_only) in order. `try_to_load_from_cache` cannot
        #: download at all, so it records as a lookup rather than as a call.
        self.calls = []
        self.lookups = []

    def snapshot_download(self, model_id, allow_patterns=None,
                          ignore_patterns=None, local_files_only=False, **kw):
        self.calls.append(("snapshot", local_files_only))
        if local_files_only and model_id not in self.cached:
            raise self.LocalEntryNotFoundError(model_id)
        return self.snapshot

    def hf_hub_download(self, repo_id=None, filename=None,
                        local_files_only=False, **kw):
        self.calls.append(("file", local_files_only))
        if local_files_only and repo_id not in self.cached:
            raise self.LocalEntryNotFoundError(repo_id)
        return self.file_path or os.path.join(self.snapshot, filename or "f")

    def try_to_load_from_cache(self, repo_id, filename, **kw):
        self.lookups.append((repo_id, filename))
        if repo_id not in self.cached:
            return None
        return self.file_path or os.path.join(self.snapshot, filename)

    class HfApi:
        def model_info(self, model_id, revision=None, files_metadata=False):
            raise AssertionError(
                "a cached model must not cost a Hub metadata call")


#: A commit sha shaped like hf's own: a snapshot directory IS one, and the record
#: is keyed off it, so a fixture named "snap" would test a key production never
#: produces.
COMMIT = "a1b2c3d4" * 5
OTHER_COMMIT = "f9e8d7c6" * 5


def _raiser(error):
    """A stand-in for a call that fails — the segmented fetch, usually, whose every
    failure degrades to hf's downloader."""
    def fail(*_args, **_kwargs):
        raise error
    return fail


def _cache_folder(tmp_path, name="models--u--x", snapshot=True, partial=False):
    """A folder shaped like hf's cache entry for one repo.

    Real directories rather than a stubbed `os.path`, because whether the cache
    holds a snapshot at all is now the gate in front of every hub call — a stub
    would prove nothing about it and would lie to pytest at the same time.
    """
    folder = tmp_path / name
    (folder / "blobs").mkdir(parents=True)
    if snapshot:
        (folder / "snapshots" / COMMIT).mkdir(parents=True)
    if partial:
        (folder / "blobs" / "abc123.incomplete").write_bytes(b"half a shard")
    return folder


def _local_hub(monkeypatch, base, hub, folder=None):
    """Install the fake library, and say what the cache folder looks like."""
    import types
    module = types.SimpleNamespace(
        snapshot_download=hub.snapshot_download,
        hf_hub_download=hub.hf_hub_download,
        try_to_load_from_cache=hub.try_to_load_from_cache,
        HfApi=hub.HfApi)
    monkeypatch.setitem(sys.modules, "huggingface_hub", module)
    monkeypatch.setattr(base, "repo_folder",
                        lambda model_id, repo_type="model":
                        None if folder is None else str(folder))


def _snapshot_dir(tmp_path, *files):
    """A resolved snapshot directory that really is there, with `files` in it.

    Named as a commit sha, because that is what hf's cache calls a snapshot and what
    `_commit_of` will accept as a key."""
    folder = tmp_path / COMMIT
    folder.mkdir(exist_ok=True)
    for name in files:
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"weights")
    return str(folder)


def test_a_CACHED_snapshot_is_resolved_with_NO_network_call(base, monkeypatch,
                                                            tmp_path):
    """The whole point: 483ms of Hub round-trips before ~14ms of weight loading,
    on every bring-up of a model already on disk. `_LocalHub.HfApi` asserts
    rather than returns, so a metadata call here fails the test by name."""
    snapshot = _snapshot_dir(tmp_path, "config.json")
    hub = _LocalHub(cached=["u/x"], snapshot=snapshot)
    folder = _cache_folder(tmp_path)
    _local_hub(monkeypatch, base, hub, folder=folder)
    # A completed fetch, recorded the way `download_snapshot` records one: the fast
    # path answers from that record and from nothing else.
    _fetched(base, folder, snapshot, commit=os.path.basename(snapshot),
             names=["config.json"])

    assert base.download_snapshot("u/x") == snapshot
    assert hub.calls == [("snapshot", True)]


def test_a_cached_FILE_is_resolved_by_a_LOOKUP_that_cannot_download(
        base, monkeypatch, tmp_path):
    """The component fetches — the 2MB speech detector, the two diarization
    models — happen INSIDE a transcription, so their 456ms each was latency on
    the way to a transcript whose bytes were already on the disk.

    Answered by `try_to_load_from_cache`, hf's read-only cache lookup, rather
    than by `hf_hub_download(local_files_only=True)`: a function that cannot
    download keeps `tests/test_ai_hub_fetch.py`'s "Stop never starts a download"
    invariant by construction instead of by an argument a later edit might drop.
    """
    snapshot = _snapshot_dir(tmp_path, "onnx/model.onnx")
    hub = _LocalHub(cached=["u/x"], snapshot=snapshot)
    _local_hub(monkeypatch, base, hub, folder=_cache_folder(tmp_path))

    assert base.download_file("u/x", "onnx/model.onnx") == os.path.join(
        snapshot, "onnx/model.onnx")
    assert hub.lookups == [("u/x", "onnx/model.onnx")]
    assert hub.calls == [], "a cached file must not reach a download function"


def test_a_repo_the_cache_has_NEVER_held_reaches_no_hub_call_at_all(
        base, monkeypatch, tmp_path):
    """The invariant `tests/test_ai_hub_fetch.py` enforces from the other side,
    pinned here at the source: with no snapshot in the cache the fast path is
    decided by a filesystem look, so nothing — not even a call carrying
    `local_files_only=True` — reaches hf before the networked path starts.

    It matters because that test counts CALLS, not downloads: "we only passed
    local_files_only" is exactly the kind of claim that stops being true when a
    refactor drops an argument, and a cancel arriving during the fetch then
    turns into a fresh multi-gigabyte download.

    A `try_to_load_from_cache` lookup IS allowed for the single-file path, and
    that is the distinction rather than an exception to it: that function has no
    download in it at all, so no argument of ours stands between it and the
    network. What must not happen is a DOWNLOAD function being called at all.
    """
    hub = _LocalHub(cached=[], snapshot=_snapshot_dir(tmp_path))
    _local_hub(monkeypatch, base, hub, folder=None)
    monkeypatch.setattr(base, "_repo_files", lambda *a, **kw: (None, None))
    monkeypatch.setattr(base, "report", lambda job=None, **fields: None)

    base.download_snapshot("u/x")

    assert hub.calls == [("snapshot", False)], hub.calls

    base.download_file("u/x", "w.bin")

    # The file path never reached a download function EXCEPT as the networked
    # fallback (`local_files_only` False), and the snapshot path reached none
    # with it True at all.
    assert [call for call in hub.calls if call[1] is True] == []


def test_a_CANCEL_out_of_the_local_attempt_is_never_read_as_not_cached(
        base, monkeypatch, tmp_path):
    """The failure `_NOT_CACHED` exists to prevent, and the one this file forbids
    everywhere else: a ✕ answered by starting a download.

    `Cancelled` is an ordinary `Exception`, so the first cut of the fast path —
    `except Exception: return None` — read it as "the cache cannot serve this"
    and went to the network. `tests/test_ai_hub_fetch.py` caught the same class of
    thing through `_segmented_fetch`; this pins the local attempt itself.
    """
    hub = _LocalHub(cached=["u/x"], snapshot=_snapshot_dir(tmp_path))

    def cancelled(*args, **kwargs):
        raise base.Cancelled()

    hub.snapshot_download = cancelled
    hub.try_to_load_from_cache = cancelled
    _local_hub(monkeypatch, base, hub, folder=_cache_folder(tmp_path))
    monkeypatch.setattr(base, "_repo_files", lambda *a, **kw: (None, None))
    monkeypatch.setattr(base, "report", lambda job=None, **fields: None)

    with pytest.raises(base.Cancelled):
        base.download_snapshot("u/x")
    with pytest.raises(base.Cancelled):
        base.download_file("u/x", "w.bin")
    assert hub.calls == [], "pressing Stop started a download instead"


def test_a_model_that_is_NOT_cached_downloads_exactly_as_before(base, monkeypatch,
                                                                tmp_path):
    """The hard constraint: first-download behaviour is untouched. The cache
    folder is there (another quantization of the same repo, say) so hf IS asked,
    it says the snapshot is not cached, and then the networked path runs — the
    metadata call, the total, the progress reporting, all of it."""
    hub = _LocalHub(cached=[], snapshot=_snapshot_dir(tmp_path))
    _local_hub(monkeypatch, base, hub, folder=_cache_folder(tmp_path))
    listed = []
    monkeypatch.setattr(base, "_repo_files",
                        lambda *a, **kw: listed.append(a) or (None, None))
    ticks = []
    monkeypatch.setattr(base, "report",
                        lambda job=None, **fields: ticks.append(fields) or None)

    assert base.download_snapshot("u/x") == hub.snapshot

    # No record for this repo, so hf is not even asked to resolve one — see
    # `_has_fetch_record`. What must still happen is the whole networked path.
    assert hub.calls == [("snapshot", False)]
    assert listed, "the networked path must still list the repo"
    assert ticks, "the networked path must still report progress"


def _blob_backed(tmp_path, folder, name, etag="e7ag", part=False, hf_part=False):
    """A snapshot entry that is a SYMLINK into `blobs/`, the way hf files one.

    The link is what makes the difference between "this file is here" and "this
    file's blob is here", and the part files beside the blob are what says a
    download is still writing it — both of which the fast path has to read
    correctly, so neither can be faked with a plain file.
    """
    blob = folder / "blobs" / etag
    blob.write_bytes(b"weights")
    if part:
        (folder / "blobs" / (etag + ".fusedpart")).write_bytes(b"half")
        (folder / "blobs" / (etag + ".fusedpart.json")).write_bytes(b"{}")
    if hf_part:
        (folder / "blobs" / (etag + ".incomplete")).write_bytes(b"half")
    snapshot = folder / "snapshots" / COMMIT
    entry = snapshot / name
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.symlink_to(blob)
    return str(snapshot), str(entry)


def test_a_part_file_for_THIS_blob_is_not_mistaken_for_a_cached_one(
        base, monkeypatch, tmp_path):
    """An interrupted download leaves our `.fusedpart` (and its offsets sidecar)
    or hf's `.incomplete` beside the blob it is writing.

    Trusting the cache then means handing `load()` a file that is still being
    written, and it must not be papered over by hf's own completeness check
    either: that check is a no-op without a cached tree listing, and the segmented
    fetch — the normal path — never writes one.
    """
    folder = _cache_folder(tmp_path, snapshot=False)
    snapshot, entry = _blob_backed(tmp_path, folder, "weights.safetensors",
                                   part=True)
    hub = _LocalHub(cached=["u/x"], snapshot=snapshot, file_path=entry)
    _local_hub(monkeypatch, base, hub, folder=folder)
    monkeypatch.setattr(base, "_repo_files", lambda *a, **kw: (None, None))
    monkeypatch.setattr(base, "report", lambda job=None, **fields: None)

    base.download_snapshot("u/x")

    assert ("snapshot", False) in hub.calls, hub.calls


@pytest.mark.parametrize("marker", ["part", "hf_part"])
def test_a_FILE_whose_blob_is_still_being_written_is_not_served(
        base, monkeypatch, tmp_path, marker):
    """The single-file half of the same rule, in its own test because the fallback
    the other one takes CLEARS our part files on its way out (`_clear_parts`, by
    design — hf is about to fetch those files itself), so asserting both in one
    test would assert the second one against a cache the first one tidied.

    Both markers are driven: ours, and hf's `.incomplete`.
    """
    folder = _cache_folder(tmp_path, snapshot=False)
    snapshot, entry = _blob_backed(tmp_path, folder, "weights.safetensors",
                                   part=marker == "part",
                                   hf_part=marker == "hf_part")
    hub = _LocalHub(cached=["u/x"], snapshot=snapshot, file_path=entry)
    _local_hub(monkeypatch, base, hub, folder=folder)
    monkeypatch.setattr(base, "_repo_files", lambda *a, **kw: (None, None))
    monkeypatch.setattr(base, "report", lambda job=None, **fields: None)

    base.download_file("u/x", "weights.safetensors")

    assert ("file", False) in hub.calls, "a blob still being written was served"


def test_a_part_file_for_an_UNRELATED_blob_does_not_disable_the_fast_path(
        base, monkeypatch, tmp_path):
    """The degradation the repo-wide scan caused, and why the check is per BLOB.

    A multi-GGUF repo (`unsloth/FLUX.2-klein-4B-GGUF` publishes a dozen
    quantizations of one model) keeps an abandoned `.fusedpart` on purpose: it is
    the resume state for the download the user cancelled (AI-5i). Scanning the
    whole repo folder let that one file disable the fast path for every unrelated,
    fully-cached file in the repo — forever, since nothing ever clears it — so the
    ~450ms this exists to remove came back precisely for the repos where
    cancelling is most likely.
    """
    folder = _cache_folder(tmp_path, snapshot=False)
    (folder / "blobs" / "0ther.fusedpart").write_bytes(b"a cancelled sibling")
    (folder / "blobs" / "0ther.incomplete").write_bytes(b"and one of hf's")
    snapshot, entry = _blob_backed(tmp_path, folder, "q4.gguf", etag="m1ne")
    hub = _LocalHub(cached=["u/x"], snapshot=snapshot, file_path=entry)
    _local_hub(monkeypatch, base, hub, folder=folder)
    _fetched(base, folder, snapshot, commit=os.path.basename(snapshot),
             names=["q4.gguf"])

    assert base.download_file("u/x", "q4.gguf") == entry
    assert hub.calls == [], hub.calls
    assert base.download_snapshot("u/x") == snapshot
    assert hub.calls == [("snapshot", True)]


def test_a_local_answer_that_is_not_actually_THERE_is_not_trusted(
        base, monkeypatch, tmp_path):
    """The path comes from a call we did not make ourselves, so it is checked
    before it is returned: a cache directory removed under a resolved ref would
    otherwise be handed to `load()` as a snapshot."""
    hub = _LocalHub(cached=["u/x"], snapshot=str(tmp_path / "went-away"))
    _local_hub(monkeypatch, base, hub, folder=_cache_folder(tmp_path))
    monkeypatch.setattr(base, "_repo_files", lambda *a, **kw: (None, None))
    monkeypatch.setattr(base, "report", lambda job=None, **fields: None)

    base.download_snapshot("u/x")

    assert ("snapshot", False) in hub.calls, hub.calls


def _fetched(base, folder, snapshot, commit=COMMIT, names=("config.json",),
             allow=None, ignore=None):
    """Record a completed fetch the way `download_snapshot` does after one.

    `snapshot` is not decoration: `_record_fetch` refuses to write a record whose
    names are not actually in the snapshot, so a fixture that skipped it would be
    recording a fetch that did not happen."""
    base._record_fetch(str(folder), commit, list(names), snapshot,
                       allow=allow, ignore=ignore)


def test_the_FALLBACK_records_the_commit_hf_actually_landed(base, monkeypatch,
                                                            tmp_path):
    """The listing's sha and hf's own answer are two resolutions of one branch
    name, and a repo that moves between them lands a different commit.

    Filed under the listing's sha, the record would name a snapshot directory that
    does not exist — cold forever for that repo, since every later fast path looks
    up a commit nothing wrote. So hf is PINNED to the commit the listing resolved,
    which is the rule `_segmented_fetch` already follows (AI-5i: the fetch is
    pinned to the commit the name resolved to, never to the name), and the record
    is then true by construction rather than by assumption.
    """
    folder = _cache_folder(tmp_path)
    snapshot = _snapshot_dir(tmp_path, "config.json")
    hub = _LocalHub(cached=[], snapshot=snapshot)
    asked = {}
    real = hub.snapshot_download

    def spy(model_id, allow_patterns=None, ignore_patterns=None,
            local_files_only=False, revision=None, **kw):
        if not local_files_only:
            asked["revision"] = revision
        return real(model_id, allow_patterns=allow_patterns,
                    ignore_patterns=ignore_patterns,
                    local_files_only=local_files_only, **kw)

    hub.snapshot_download = spy
    _local_hub(monkeypatch, base, hub, folder=folder)
    monkeypatch.setattr(base, "_repo_files",
                        lambda *a, **kw: (COMMIT, [("config.json", 7)]))
    monkeypatch.setattr(base, "_segmented_fetch", _raiser(RuntimeError("no ranges")))
    monkeypatch.setattr(base, "report", lambda job=None, **fields: None)

    base.download_snapshot("u/x")

    assert asked == {"revision": COMMIT}, asked


def test_a_TORN_record_left_by_a_crashed_write_is_not_read_as_a_record(
        base, monkeypatch, tmp_path):
    """`_record_fetch` writes a per-writer temp and `os.replace`s it, so a crash
    mid-write leaves the temp behind. Matching that as a record made a record-LESS
    repo pay a hub resolve on every single download — the cold path plus a round
    trip — so the suffix is excluded from the match.

    Excluded rather than deleted: the same name is what a fetch in flight in another
    process is writing, and the sweep this used to do is covered by
    `test_a_temp_from_ANOTHER_writer_is_left_ALONE`.
    """
    folder = _cache_folder(tmp_path)
    torn = folder / base._temp_record(base._FETCH_RECORD % COMMIT)
    torn.write_text('{"commit": "%s", "sco' % COMMIT)
    hub = _LocalHub(cached=["u/x"], snapshot=_snapshot_dir(tmp_path, "config.json"))
    _local_hub(monkeypatch, base, hub, folder=folder)
    monkeypatch.setattr(base, "_repo_files", lambda *a, **kw: (None, None))
    monkeypatch.setattr(base, "report", lambda job=None, **fields: None)

    assert base._has_fetch_record(str(folder)) is False

    base.download_snapshot("u/x")
    assert [call for call in hub.calls if call[1] is True] == [], hub.calls


def test_a_fetch_that_landed_LESS_than_its_scope_writes_NO_record(
        base, monkeypatch, tmp_path):
    """The inverse of the failure the record exists to prevent, and the reason the
    record's file list comes from the LISTING rather than from the disk.

    A fallback that lands 1 of 50 files — hf's `filter_repo_objects` disagreeing
    with `selects`, or a fetch that finished partially — would record ONE name if
    the record were built from what landed. A record is verified by looking its own
    names up, so that record would be self-certifying: every later check passes and
    the fast path serves an incomplete snapshot forever. Checked instead against the
    set the listing asked for — the one thing here the fetch did not choose — a
    shortfall writes nothing, and a repo with no record is merely cold, which is
    where it was before this path existed. This also supersedes recording "what
    landed": the disagreement it was meant to survive now leaves no record instead
    of a wrong one.
    """
    folder = _cache_folder(tmp_path)
    # The listing asked for two; only one is on disk.
    snapshot = _snapshot_dir(tmp_path, "config.json")
    hub = _LocalHub(cached=[], snapshot=snapshot)
    _local_hub(monkeypatch, base, hub, folder=folder)
    monkeypatch.setattr(base, "_repo_files", lambda *a, **kw: (
        COMMIT, [("config.json", 7), ("weights.safetensors", 9)]))
    monkeypatch.setattr(base, "_segmented_fetch", _raiser(RuntimeError("no ranges")))
    monkeypatch.setattr(base, "report", lambda job=None, **fields: None)

    base.download_snapshot("u/x")

    assert base._recorded_files(str(folder), COMMIT, None, None) is None
    assert not base._has_fetch_record(str(folder)), \
        "an incomplete fetch must leave no record at all"

    # …so the next download is the networked one, not a served snapshot.
    hub.calls.clear()
    base.download_snapshot("u/x")
    assert [call for call in hub.calls if call[1] is True] == [], hub.calls


def test_a_temp_from_ANOTHER_writer_is_left_ALONE(base, monkeypatch, tmp_path):
    """A `.writing` temp is the name a fetch in flight is writing RIGHT NOW, and two
    model loads sharing one HF cache are separate processes with no lock between
    them.

    Unlinking one to save a round trip made the other process's `os.replace` fail,
    so its record was never written and its repo stayed permanently cold — the exact
    failure the record exists to prevent, caused by tidying up. The temp is
    therefore unique per writer and merely IGNORED here; each writer cleans up only
    its own.
    """
    folder = _cache_folder(tmp_path)
    theirs = folder / (base._FETCH_RECORD % COMMIT + ".99999" + base._RECORD_TEMP)
    theirs.write_text('{"commit": "half a record')

    assert base._has_fetch_record(str(folder)) is False
    assert theirs.exists(), "another writer's in-flight record was deleted"

    # And our own temp name is not theirs, so neither can clobber the other.
    assert base._temp_record(base._FETCH_RECORD % COMMIT) != theirs.name


def test_two_writers_in_one_pid_still_get_DIFFERENT_temp_names(base):
    """The pid is not enough on its own, and the failure it leaves is worse than the
    one it replaced.

    Two containers sharing a mounted HF cache have their own pid namespaces, and a
    pid is reused after a crash anyway — so `.fused-fetch-<sha>.json.<pid>.writing`
    can name one file that two writers interleave into, and `os.replace` then
    publishes mixed JSON as TRUTH. The sweep this design replaced only ever cost a
    cold repo; that would cost a record the fast path believes. A random token per
    call makes "a temp is distinguishable from another writer's" actually true.
    """
    record = base._FETCH_RECORD % ("a" * 40)
    names = {base._temp_record(record) for _ in range(50)}

    assert len(names) == 50, "temp names must not collide"
    assert all(name.startswith(record) for name in names)
    assert all(name.endswith(base._RECORD_TEMP) for name in names)


def test_a_SKIPPED_record_reads_as_a_diagnostic_not_as_a_failed_download(
        base, tmp_path, capsys):
    """The shortfall line fires on a download that WORKED.

    `selects` and hf's `filter_repo_objects` disagreeing by one name is the very
    thing this file calls a real possibility, and when it happens the weights are on
    disk and the load is about to succeed — so a message shaped like an error had a
    user reading a perfect download as a broken one. The silence it replaced was the
    original problem, so the line stays and says what is true instead: the download
    is fine, only the fast-path record was skipped, and the next load re-resolves
    over the network.
    """
    folder = _cache_folder(tmp_path)
    snapshot = _snapshot_dir(tmp_path, "config.json")

    base._record_fetch(str(folder), COMMIT, ["config.json", "absent.bin"], snapshot)

    said = capsys.readouterr().err
    assert "absent.bin" in said, "the diagnostic must name what it could not find"
    assert "download" in said.lower()
    for reassurance in ("succeeded", "not a failure"):
        assert reassurance in said.lower(), said
    assert not base._has_fetch_record(str(folder))


def test_a_record_is_never_written_for_a_snapshot_with_NO_path(
        base, tmp_path, monkeypatch, capsys):
    """`os.path.join(snapshot or "", name)` made every check CWD-relative when the
    path was falsy, so a process whose working directory happened to hold a matching
    name — `config.json` is not far-fetched — passed the shortfall check and wrote a
    record for a snapshot whose location was never known.

    A missing path is missing, and belongs in the early return rather than papered
    over with a default.

    **And it is NAMED on stderr**, unlike the ordinary nothings beside it (no cache
    folder, a revision that is not a commit, a listing that selected nothing). Both
    callers reach the writer only after a fetch returned, so a fetch that returned no
    path is a bug in that file rather than a shape the world produces — and a repo
    left permanently cold with no diagnostic is the invisibility the stderr line
    exists to prevent. The first version of this test asserted silence here, which
    contradicted the docstring one paragraph above the code; all three now agree.
    """
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    (elsewhere / "config.json").write_bytes(b"{}")
    monkeypatch.chdir(elsewhere)
    folder = _cache_folder(tmp_path)

    base._record_fetch(str(folder), COMMIT, ["config.json"], None)
    base._record_fetch(str(folder), COMMIT, ["config.json"], "")

    assert not base._has_fetch_record(str(folder)), \
        "a record was written for a snapshot nobody located"
    said = capsys.readouterr().err
    assert said.count("no snapshot path") == 2, said
    # …and it reads as the diagnostic it is, not as a broken download.
    assert "succeeded" in said and "not a failure" in said, said


def test_the_ORDINARY_nothings_are_declined_in_SILENCE(base, tmp_path, capsys):
    """The other half of that decision, so the line above cannot creep into noise.

    No cache folder is a venv without huggingface_hub, a commit of None is
    `_commit_of` refusing a path that is not a sha — every `local_dir` download — and
    an empty name list is a listing that selected nothing. Those are shapes the world
    produces, not signs a fetch went wrong, and a stderr line on each would train a
    user to ignore the two that mean something."""
    snapshot = _snapshot_dir(tmp_path, "config.json")

    base._record_fetch(None, COMMIT, ["config.json"], snapshot)
    base._record_fetch(str(tmp_path), None, ["config.json"], snapshot)
    base._record_fetch(str(tmp_path), COMMIT, [], snapshot)

    assert capsys.readouterr().err == ""


def test_a_caller_supplied_REVISION_wins_over_the_pin(base, monkeypatch, tmp_path):
    """The pin is this file's default, not an override of the caller.

    Splatting the pin and `kwargs` into one call raised `TypeError: got multiple
    values for keyword argument 'revision'` — masked only because a call carrying
    `kwargs` returns before the pin is applied, which made it a crash waiting on a
    reorder rather than a non-issue. The caller's revision wins, and nothing raises.
    """
    folder = _cache_folder(tmp_path)
    snapshot = _snapshot_dir(tmp_path, "config.json")
    hub = _LocalHub(cached=[], snapshot=snapshot)
    seen = {}
    real = hub.snapshot_download

    def spy(model_id, revision=None, local_files_only=False, **kw):
        if not local_files_only:
            seen["revision"] = revision
        return real(model_id, local_files_only=local_files_only, **kw)

    hub.snapshot_download = spy
    _local_hub(monkeypatch, base, hub, folder=folder)
    monkeypatch.setattr(base, "_repo_files", lambda *a, **kw: (COMMIT, []))
    monkeypatch.setattr(base, "report", lambda job=None, **fields: None)

    assert base.download_snapshot("u/x", revision="theirs") == snapshot
    assert seen == {"revision": "theirs"}, seen


def test_a_path_that_is_not_a_COMMIT_records_nothing(base, monkeypatch, tmp_path):
    """`_commit_of` produces a KEY, and the empty string is a key that reads back.

    A path with no basename made the writer file `.fused-fetch-.json` and the reader
    look up the very same name, so a record written under nothing at all came back
    as a hit — the opposite of the miss the docstring promises. Anything that is not
    a plausible commit is None, and both writers skip.
    """
    assert base._commit_of(None) is None
    assert base._commit_of("") is None
    assert base._commit_of("/cache/snapshots/") is None
    assert base._commit_of("not-a-sha") is None
    assert base._commit_of("/cache/snapshots/" + COMMIT) == COMMIT

    folder = _cache_folder(tmp_path)
    plain = tmp_path / "not-a-commit"
    plain.mkdir()
    (plain / "config.json").write_bytes(b"{}")
    hub = _LocalHub(cached=[], snapshot=str(plain))
    _local_hub(monkeypatch, base, hub, folder=folder)
    monkeypatch.setattr(base, "_repo_files",
                        lambda *a, **kw: (COMMIT, [("config.json", 7)]))
    monkeypatch.setattr(base, "_segmented_fetch", lambda *a, **kw: str(plain))
    monkeypatch.setattr(base, "report", lambda job=None, **fields: None)

    base.download_snapshot("u/x")

    assert not base._has_fetch_record(str(folder)), \
        "a record under a non-commit key is one that reads back as a hit"


def test_a_snapshot_fetched_at_a_NARROWER_scope_does_not_answer_a_WIDER_request(
        base, monkeypatch, tmp_path):
    """The hole the "skip scoped calls" rule did not close, and it is reachable
    today rather than hypothetical.

    Scoping is a property of the on-disk STATE, not of the call: `diffusers_image`
    fetches `black-forest-labs/FLUX.2-klein-4B` with
    `allow_patterns=list(recipe["keep"])`, and the SAME id reaches an UNSCOPED
    `download_snapshot` through `mflux_image.download` — `POST /api/ai/runtime/
    download` resolves the runner from the user's image-engine preference and
    `weights_only=True` stops before the `load()` that would refuse the format. So
    a user who downloads on the Diffusers engine and then downloads again on the
    MLX FLUX engine asks for the whole repo against a cache holding a tenth of it.
    Answered from the cache, that download reports success having fetched nothing.

    The same flip happens with no user action at all if a recipe is ever REMOVED
    from `_GGUF_RECIPES`: `download()` takes its unscoped branch against a cache
    that is still scoped, and `from_pretrained` fails on a component that was
    never fetched.

    So the scope that was FETCHED is recorded, and the fast path answers only a
    request for the same one.
    """
    folder = _cache_folder(tmp_path)
    snapshot = _snapshot_dir(tmp_path, "config.json")
    hub = _LocalHub(cached=["u/x"], snapshot=snapshot)
    _local_hub(monkeypatch, base, hub, folder=folder)
    monkeypatch.setattr(base, "_repo_files", lambda *a, **kw: (None, None))
    monkeypatch.setattr(base, "report", lambda job=None, **fields: None)
    _fetched(base, folder, snapshot, commit=os.path.basename(snapshot),
             names=["config.json"], allow=["*.json"])

    base.download_snapshot("u/x")

    assert ("snapshot", False) in hub.calls, hub.calls


def test_a_snapshot_fetched_at_the_SAME_scope_answers_from_the_cache(
        base, monkeypatch, tmp_path):
    """The other side: a scoped call is not refused on principle — it is answered
    when this app recorded a completed fetch of that exact scope for that exact
    commit, which is a claim about the disk rather than an argument about it."""
    folder = _cache_folder(tmp_path)
    snapshot = _snapshot_dir(tmp_path, "config.json", "vae/diffusion.safetensors")
    hub = _LocalHub(cached=["u/x"], snapshot=snapshot)
    _local_hub(monkeypatch, base, hub, folder=folder)
    _fetched(base, folder, snapshot, commit=os.path.basename(snapshot),
             names=["config.json", "vae/diffusion.safetensors"],
             allow=["*.json", "vae/*"])

    got = base.download_snapshot("u/x", allow_patterns=["*.json", "vae/*"])

    assert got == snapshot
    assert hub.calls == [("snapshot", True)]


def test_a_recorded_file_that_WENT_AWAY_sends_the_download_back_to_the_hub(
        base, monkeypatch, tmp_path):
    """Recording the names is what makes a MISSING file detectable at all. A blob
    deleted with its snapshot entry — `hf cache` pruning, a tidy cleanup script —
    leaves nothing for a walk of the snapshot to trip over, and the fast path
    would report a model that cannot load. Before it existed, pressing Download
    again re-listed and re-fetched exactly this."""
    folder = _cache_folder(tmp_path)
    snapshot = _snapshot_dir(tmp_path, "config.json")
    hub = _LocalHub(cached=["u/x"], snapshot=snapshot)
    _local_hub(monkeypatch, base, hub, folder=folder)
    monkeypatch.setattr(base, "_repo_files", lambda *a, **kw: (None, None))
    monkeypatch.setattr(base, "report", lambda job=None, **fields: None)
    # Recorded through the writer's own rule, so the missing file has to be planted
    # AFTER the record exists: this is a file that went away, not one that never
    # arrived (which `_record_fetch` would have refused to record at all).
    (tmp_path / os.path.basename(snapshot) / "weights.safetensors").write_bytes(b"w")
    _fetched(base, folder, snapshot, commit=os.path.basename(snapshot),
             names=["config.json", "weights.safetensors"])
    os.remove(os.path.join(snapshot, "weights.safetensors"))

    base.download_snapshot("u/x")

    assert ("snapshot", False) in hub.calls, hub.calls


def test_a_repo_with_NO_record_takes_the_networked_path_and_gains_one(
        base, monkeypatch, tmp_path):
    """What every cache looks like the first time this code runs on a machine that
    already holds models — and the migration, which needs no special case: the
    networked path completes as it always did and records what it fetched, so the
    NEXT bring-up is the fast one. A cache with no record is never served."""
    folder = _cache_folder(tmp_path)
    snapshot = _snapshot_dir(tmp_path, "config.json")
    hub = _LocalHub(cached=["u/x"], snapshot=snapshot)
    _local_hub(monkeypatch, base, hub, folder=folder)
    monkeypatch.setattr(base, "_repo_files",
                        lambda *a, **kw: (COMMIT, [("config.json", 7)]))
    monkeypatch.setattr(base, "_segmented_fetch",
                        lambda model_id, names, sha, **kw: snapshot)
    monkeypatch.setattr(base, "report", lambda job=None, **fields: None)

    assert base.download_snapshot("u/x") == snapshot
    assert [call for call in hub.calls if call[1] is True] == [], hub.calls

    # …and the record it just wrote answers the next call off the disk.
    hub.calls.clear()
    assert base.download_snapshot("u/x") == snapshot
    assert hub.calls == [("snapshot", True)]


def test_a_snapshot_with_a_DANGLING_entry_sends_the_download_back_to_the_hub(
        base, monkeypatch, tmp_path):
    """The self-repair this path must not take away.

    A blob removed under a complete-looking snapshot — `hf cache` pruning, a
    partial copy of somebody's cache, a cleanup script — leaves the snapshot's
    symlink pointing at nothing. Before the fast path, pressing Download again
    re-listed the repo and re-fetched what was missing; a fast path that returns
    the snapshot folder unread makes that unrepairable, and every load from then
    on fails on a file the cache claims to have.

    hf's own completeness check does NOT cover this: `_raise_if_incomplete_
    snapshot` is a no-op unless `trees/<commit>.json` is cached, and this app's
    segmented fetch (the normal path — hf's downloader runs only on fallback)
    writes `refs/` and the blobs, never a tree listing.
    """
    snapshot = tmp_path / "snap"
    snapshot.mkdir()
    (snapshot / "config.json").write_bytes(b"{}")
    (snapshot / "weights.safetensors").symlink_to(tmp_path / "gone-blob")
    hub = _LocalHub(cached=["u/x"], snapshot=str(snapshot))
    _local_hub(monkeypatch, base, hub, folder=_cache_folder(tmp_path))
    monkeypatch.setattr(base, "_repo_files", lambda *a, **kw: (None, None))
    monkeypatch.setattr(base, "report", lambda job=None, **fields: None)

    base.download_snapshot("u/x")

    assert ("snapshot", False) in hub.calls, hub.calls


def test_an_EMPTY_snapshot_directory_is_not_a_cached_model(base, monkeypatch,
                                                           tmp_path):
    """A directory with nothing in it satisfies every existence check and holds no
    weights. It is what an interrupted first download can leave once its part
    files are cleared, and returning it would hand `load()` a path with no model
    at it."""
    empty = tmp_path / "snap"
    empty.mkdir()
    hub = _LocalHub(cached=["u/x"], snapshot=str(empty))
    _local_hub(monkeypatch, base, hub, folder=_cache_folder(tmp_path))
    monkeypatch.setattr(base, "_repo_files", lambda *a, **kw: (None, None))
    monkeypatch.setattr(base, "report", lambda job=None, **fields: None)

    base.download_snapshot("u/x")

    assert ("snapshot", False) in hub.calls, hub.calls


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
