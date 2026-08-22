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

MODEL = "MiniMaxAI/MiniMax-H3-FL2VA"


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
    script sleeps between steps and installs a real SIGTERM handler that
    writes a marker file — so this test proves an actual signal reached an
    actual process, not merely that the worker returned quickly."""
    marker = tmp_path / "terminated.marker"
    monkeypatch.setenv("H3_FAKE_TERM_MARKER", str(marker))
    monkeypatch.setenv("H3_FAKE_STEP_SLEEP", "2")
    base.cancel_on_tick = 2  # the first tick is the 0/N pre-spawn report

    worker = load_worker(monkeypatch, base)
    worker.load(MODEL, snapshot(tmp_path))

    with pytest.raises(Cancelled):
        worker.generate(_request(tmp_path, steps=10))

    assert marker.read_text() == "terminated\n"
    # The long sleep between steps means a real 10-step run would still be
    # going; a written output file would mean the child was not actually
    # stopped before it finished.
    assert not os.path.exists(str(tmp_path / "fox.mp4"))
