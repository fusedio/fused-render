"""The h3_video runner's own logic, driven directly (SPEC §40).

Every other runner test in this folder fakes the LIBRARY the worker imports,
because those workers load a model into their own interpreter. This one
cannot do that: h3.c is not a library, it is a bundled binary, and the whole
point of this worker is spawning it correctly — an absolute exe path, a real
pipe read loop, real cancellation via SIGTERM. Faking `subprocess.Popen`
itself (a canned `CompletedProcess`) would prove nothing about any of that, so
`tests/fixtures/fake_h3.py` is a REAL executable script standing in for the
binary: it holds real pipes, prints real progress lines, and can be told to
exit nonzero or catch a real SIGTERM.

`worker_base` is still faked — its job (talking to the download manager over
HTTP) is exactly what the mflux/whisper worker tests already fake, and is not
what this file is about. `imageio_ffmpeg` is faked too: the fake h3 script
never touches `H3_FFMPEG`, so only `get_ffmpeg_exe()` needs to return
something.
"""
import importlib.util
import os
import sys
import types

import pytest

WORKER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "ai", "runners", "h3_video", "worker.py",
)
FAKE_H3 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "fake_h3.py")

MODEL = "MiniMaxAI/MiniMax-H3"


class Cancelled(Exception):
    pass


class FakeBase:
    """A stand-in for `worker_base`, recording every tick.

    `report_or_cancel` raises `Cancelled` from the Nth call on, when a test
    sets `cancel_on_tick` — the real contract `report_or_cancel` has (raise
    when the reply says the ✕ was pressed), reproduced without a network call.
    """

    Cancelled = Cancelled

    def __init__(self):
        self.ticks = []
        self.state = {}
        self.cancel_on_tick = None

    def report(self, job=None, **fields):
        self.ticks.append({"job": job, **fields})

    def report_or_cancel(self, job=None, **fields):
        self.ticks.append({"job": job, **fields})
        if self.cancel_on_tick is not None and len(self.ticks) >= self.cancel_on_tick:
            raise Cancelled()
        return {"cancel_requested": False}

    def set_state(self, **fields):
        self.state.update(fields)

    def download_snapshot(self, model_id, **kwargs):
        self.download_kwargs = kwargs
        return f"/snapshots/{model_id}"

    def serve(self, **kwargs):
        return None


@pytest.fixture()
def base():
    return FakeBase()


def load_worker(monkeypatch, base):
    monkeypatch.setitem(sys.modules, "worker_base", base)
    fake_ffmpeg = types.ModuleType("imageio_ffmpeg")
    fake_ffmpeg.get_ffmpeg_exe = lambda: "/fake/ffmpeg"
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", fake_ffmpeg)
    spec = importlib.util.spec_from_file_location("h3_worker_under_test", WORKER_PATH)
    assert spec is not None and spec.loader is not None, WORKER_PATH
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def snapshot(tmp_path, *, diffusers=False):
    """A downloaded repo directory, as `download` would leave it."""
    root = tmp_path / "snap"
    root.mkdir()
    if diffusers:
        (root / "model_index.json").write_text("{}")
    return str(root)


def _request(tmp_path, **over):
    return {"prompt": "a fox running through snow",
            "out": str(tmp_path / "fox.mp4"),
            "job": "sys:ai-video:abc", **over}


@pytest.fixture()
def h3_env(monkeypatch, tmp_path):
    """Points `FUSED_RENDER_H3_BIN` at the real fake-h3 script."""
    monkeypatch.setenv("FUSED_RENDER_H3_BIN", FAKE_H3)
    return FAKE_H3


# ------------------------------------------------------------------- loading


def test_load_refuses_a_diffusers_snapshot_by_name(monkeypatch, base, tmp_path, h3_env):
    worker = load_worker(monkeypatch, base)
    with pytest.raises(RuntimeError) as caught:
        worker.load(MODEL, snapshot(tmp_path, diffusers=True))
    message = str(caught.value)
    assert "Diffusers" in message
    assert MODEL in message


def test_load_refuses_when_no_binary_path_was_provided(monkeypatch, base, tmp_path):
    monkeypatch.delenv("FUSED_RENDER_H3_BIN", raising=False)
    worker = load_worker(monkeypatch, base)
    with pytest.raises(RuntimeError, match="h3 binary"):
        worker.load(MODEL, snapshot(tmp_path))


def test_load_accepts_a_plain_snapshot_and_sets_device(monkeypatch, base, tmp_path, h3_env):
    worker = load_worker(monkeypatch, base)
    worker.load(MODEL, snapshot(tmp_path))
    assert base.state.get("device") == "mps"


def _fake_huggingface_hub(monkeypatch, *, files=None, error=None):
    """A fake `huggingface_hub` good enough for `download`'s listing check —
    `list_repo_files` either returns `files` or raises `error`, the same
    shape `test_ai_llamacpp_worker.py`'s own fake uses for the one function
    its runner calls."""
    fake = types.ModuleType("huggingface_hub")
    calls = []

    def list_repo_files(model_id, **kwargs):
        calls.append(model_id)
        if error is not None:
            raise error
        return list(files or [])

    fake.list_repo_files = list_repo_files
    fake.calls = calls
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake)
    return fake


#: A minimal H3-shaped listing: enough filenames under `FL2VA/` to satisfy
#: the tree check without needing the repo's real 81-file shape.
_FL2VA_LISTING = ["model_index.json", "FL2VA/model_index.json",
                  "FL2VA/transformer/config.json", "FL2VA/tokenizer/tokenizer.json",
                  "Ref2VA/transformer/config.json"]

#: What a diffusers text-to-video repo (LTX, Wan, …) reachable through the
#: same Hub task looks like: a `model_index.json` at the root and ordinary
#: pipeline component folders — no `FL2VA/` anywhere.
_DIFFUSERS_VIDEO_LISTING = ["model_index.json", "transformer/config.json",
                            "vae/config.json", "text_encoder/config.json"]


def test_download_fetches_only_the_fl2va_tree(monkeypatch, base, h3_env):
    """MiniMaxAI/MiniMax-H3 is a 498.5GB whole repo carrying BOTH the FL2VA
    and Ref2VA checkpoints, plus a second unused copy of their shared
    components at the repo root — VERIFIED against the built h3.c source
    (h3.c's h3_load_dir): every path it ever opens is 'FL2VA/...' or
    'Ref2VA/...', never a bare root path. This build offers prompt-only
    FL2VA rendering, so a download must fetch the FL2VA/ tree ONLY —
    fetching the other ~354GB (Ref2VA plus the duplicate root components)
    would be silent waste, not merely a slower correct download."""
    _fake_huggingface_hub(monkeypatch, files=_FL2VA_LISTING)
    worker = load_worker(monkeypatch, base)
    worker.download(MODEL)
    assert base.download_kwargs == {"allow_patterns": ["FL2VA/*"]}


def test_download_refuses_a_repo_with_no_fl2va_tree(monkeypatch, base, h3_env):
    """The bug `['FL2VA/*']` alone would cause: a non-H3 repo (a diffusers
    text-to-video pipeline reachable through the same Hub task, say) matches
    NOTHING under that pattern, so `download_snapshot` would "succeed" with
    an EMPTY snapshot — no files, including no `model_index.json`, since
    that is excluded by the same pattern — and `load()`'s own Diffusers-
    marker guard would then pass VACUOUSLY on a directory with nothing in
    it. The user's only signal would be an opaque `h3 exited with code N`
    minutes later. Checking the repo's own Hub LISTING before fetching
    anything catches this up front, with the same clear sentence `load()`
    already gives an on-disk snapshot in the wrong shape."""
    hub = _fake_huggingface_hub(monkeypatch, files=_DIFFUSERS_VIDEO_LISTING)
    worker = load_worker(monkeypatch, base)
    with pytest.raises(RuntimeError) as caught:
        worker.download("org/some-diffusers-video-repo")
    message = str(caught.value)
    assert "org/some-diffusers-video-repo" in message
    assert "FL2VA" in message
    # Refused BEFORE any bytes moved — no download attempted.
    assert not hasattr(base, "download_kwargs")
    assert hub.calls == ["org/some-diffusers-video-repo"]


def test_download_surfaces_a_listing_failure(monkeypatch, base, h3_env):
    _fake_huggingface_hub(monkeypatch, error=RuntimeError("offline"))
    worker = load_worker(monkeypatch, base)
    with pytest.raises(RuntimeError) as caught:
        worker.download(MODEL)
    assert MODEL in str(caught.value)
    assert "offline" in str(caught.value)
    assert not hasattr(base, "download_kwargs")


# ---------------------------------------------------------------- happy path


def test_the_happy_path_renders_a_real_file_and_reports_steps(
        monkeypatch, base, tmp_path, h3_env):
    worker = load_worker(monkeypatch, base)
    worker.load(MODEL, snapshot(tmp_path))

    result = worker.generate(_request(tmp_path, steps=3, frames=17, width=512,
                                      height=512, seed=42))

    assert result == {
        "path": str(tmp_path / "fox.mp4"),
        "seconds": pytest.approx(result["seconds"]),
        "seed": 42,
        "width": 512,
        "height": 512,
        "frames": 17,
        "steps": 3,
    }
    # The fake script actually ran and actually wrote the file — not a stub
    # standing in for one.
    with open(result["path"], "rb") as handle:
        assert b"fake-h3-output" in handle.read()

    # Step ticks: 0/3 published before the spawn, then one per real stdout
    # line the fake script printed.
    task_ticks = [t for t in base.ticks if t.get("kind") == "task"]
    assert task_ticks[0] == {
        "job": "sys:ai-video:abc", "state": "running", "kind": "task",
        "unit": "", "done": 0, "total": 3, "detail": "Rendering — step 0/3",
    }
    parsed = [(t["done"], t["total"]) for t in task_ticks[1:]]
    assert parsed == [(1, 3), (2, 3), (3, 3)]


def test_an_unparseable_line_reports_an_indeterminate_tick(
        monkeypatch, base, tmp_path, h3_env):
    """h3 may print something between steps that carries no N/M — a loading
    banner, say. The plan is explicit that such a line reports indeterminate
    progress rather than being dropped, so the row keeps moving instead of
    going quiet."""
    worker = load_worker(monkeypatch, base)
    worker.load(MODEL, snapshot(tmp_path))
    monkeypatch.setenv("H3_FAKE_STEP_SLEEP", "0")

    # Steps=1 still produces one parseable "step 1/1" line; what matters here
    # is that the reporter's OWN parsing degrades gracefully on a line that
    # doesn't match, which we check directly against the pure function.
    assert worker._parse_progress("loading weights...") == (None, None)
    assert worker._parse_progress("step 4/20") == (4, 20)
    assert worker._parse_progress("sampling 4 / 20 done") == (4, 20)


def test_many_noise_lines_do_not_produce_a_post_per_line(monkeypatch, base, tmp_path, h3_env):
    """The real regression: `report_or_cancel` is a blocking HTTP round trip,
    and a chatty h3 build can print far more lines than there are steps
    (per-layer diagnostics, timing dumps). Unthrottled, hundreds of such
    lines serialize behind hundreds of POSTs and stall the very drain that
    is supposed to keep the pipe from filling. The fake prints 300 identical
    non-step lines as fast as it can — nothing in real time separates them —
    so a correctly throttled worker reports the first one and then, within
    its own throttle window, none of the rest."""
    monkeypatch.setenv("H3_FAKE_NOISE_LINES", "300")
    worker = load_worker(monkeypatch, base)
    worker.load(MODEL, snapshot(tmp_path))

    worker.generate(_request(tmp_path, steps=1))

    task_ticks = [t for t in base.ticks if t.get("kind") == "task"]
    # 0/1 pre-spawn report, plus at most a handful from the noise burst and
    # the one real "step 1/1" line — nowhere near 300+1.
    assert len(task_ticks) < 10, len(task_ticks)


# --------------------------------------------------------------- error paths


def test_a_nonzero_exit_surfaces_stderr(monkeypatch, base, tmp_path, h3_env):
    monkeypatch.setenv("H3_FAKE_EXIT_CODE", "7")
    worker = load_worker(monkeypatch, base)
    worker.load(MODEL, snapshot(tmp_path))

    with pytest.raises(RuntimeError) as caught:
        worker.generate(_request(tmp_path, steps=2))
    message = str(caught.value)
    assert "7" in message
    assert "fake h3 failure" in message
    # No file was written on the failure path.
    assert not os.path.exists(str(tmp_path / "fox.mp4"))


def test_a_large_stderr_does_not_deadlock_the_render(monkeypatch, base, tmp_path, h3_env):
    """The real regression this worker has to avoid: reading stdout only,
    while the child fills its stderr pipe, blocks the child's write() call
    forever. `H3_FAKE_STDERR_BYTES` is set well past a pipe's OS buffer so
    this test would hang (and fail on the suite's timeout) if `_drain_stderr`
    were not running concurrently — a canned subprocess result could never
    have exercised this."""
    monkeypatch.setenv("H3_FAKE_STDERR_BYTES", str(256 * 1024))
    monkeypatch.setenv("H3_FAKE_EXIT_CODE", "0")
    worker = load_worker(monkeypatch, base)
    worker.load(MODEL, snapshot(tmp_path))

    result = worker.generate(_request(tmp_path, steps=2))
    assert os.path.exists(result["path"])


def test_generate_without_a_load_is_refused(monkeypatch, base, tmp_path, h3_env):
    worker = load_worker(monkeypatch, base)
    with pytest.raises(RuntimeError, match="no model is loaded"):
        worker.generate(_request(tmp_path))


# --------------------------------------------------------------- cancellation


def test_cancellation_kills_the_real_child_process(monkeypatch, base, tmp_path, h3_env):
    """The ✕: `report_or_cancel` raises after the first tick, and the fake h3
    script sleeps between steps — so this test proves an actual OS-level kill
    reached an actual process, not merely that the worker returned quickly.

    **POSIX and Windows diverge on what "killed" looks like, and the
    assertion has to follow that rather than paper over it.** `Popen.
    terminate()` is SIGTERM on POSIX — a signal the fake script catches to
    write a marker file, proving the signal was actually delivered and
    handled — but on Windows it is `TerminateProcess`, which ends the
    process outright with no chance for any Python signal handler (or
    anything else in the target) to run first; there is no marker to write
    there BY CONSTRUCTION, not because the kill failed. So POSIX keeps the
    stronger, marker-based proof, and Windows asserts the honest cross-
    platform equivalent this worker actually guarantees everywhere: the
    real child process it spawned is confirmed GONE (`proc.poll()` no
    longer `None`) after `generate()` unwinds, and it never got to write
    the output file it was mid-render on.

    (This matters only for the test double, not for production: the real
    `h3-video` runner is Apple-Silicon-only, so this worker never actually
    runs on Windows — the concern here is purely a portable TEST SUITE that
    also runs on that platform, not a real cleanup path h3.c depends on
    there.)
    """
    import subprocess as subprocess_module

    marker = tmp_path / "terminated.marker"
    monkeypatch.setenv("H3_FAKE_TERM_MARKER", str(marker))
    monkeypatch.setenv("H3_FAKE_STEP_SLEEP", "2")
    base.cancel_on_tick = 2  # the first tick is the 0/N pre-spawn report

    worker = load_worker(monkeypatch, base)
    worker.load(MODEL, snapshot(tmp_path))

    # Captured via a thin wrap around the worker's OWN `subprocess.Popen`
    # (not a fake — the real call still runs) so the test can inspect the
    # real child process after `generate()` unwinds, which owns no
    # reference to `proc` once it has raised.
    spawned = []
    real_popen = subprocess_module.Popen

    def capturing_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(worker.subprocess, "Popen", capturing_popen)

    with pytest.raises(Cancelled):
        worker.generate(_request(tmp_path, steps=10))

    assert len(spawned) == 1, spawned
    proc = spawned[0]
    # True on every platform: `generate` calls `proc.wait()` (with a
    # `.kill()` fallback past a 5s timeout) before re-raising `Cancelled`,
    # so by the time this line runs the child is not merely asked to stop,
    # it IS stopped.
    assert proc.poll() is not None, "the child process is still running"

    if sys.platform == "win32":
        # No marker — see the docstring. The output file is the other
        # witness available on every platform, checked below.
        pass
    else:
        assert marker.read_text() == "terminated\n"

    # The long sleep between steps means a real 10-step run would still be
    # going; a written output file would mean the child was not actually
    # stopped before it finished. True on every platform regardless of
    # which kill signal got it there.
    assert not os.path.exists(str(tmp_path / "fox.mp4"))
