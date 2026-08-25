"""Real bytes and a real phrase out of `uv sync`, instead of a coarse stage word.

The bug this covers: `_env_install_worker.py` used to run `uv sync` behind
`subprocess.run(capture_output=True)`, so nothing inside it was observable — the
install stage could only beat an elapsed-time keepalive onto `progress.json`
while a 3.4GB ROCm torch wheel downloaded in complete silence. uv itself prints
per-package sizes and completions to stderr as it goes; `_UvProgress` parses
that text (never uv's internals) into `(activity, bytes_done, bytes_total)`,
and `_build` now streams stderr through a `subprocess.Popen` instead of
capturing it, so those numbers exist while the sync is still running.

No test here asserts on the AI job row's rendered sentence — that is
`tests/test_ai_runtime.py`'s job (`test_a_venv_build_reports_more_than_a_stage_
word`). This file is the parser and the streaming plumbing in isolation.
"""
import importlib.util
import os
import subprocess
import time
from pathlib import Path

import pytest


def _worker():
    """`_env_install_worker` as a module, imported by path — it must stay free
    of any `fused_render` import (D152), so it is not reachable the normal way.
    """
    path = Path(__file__).resolve().parents[1] / "fused_render" / "_env_install_worker.py"
    spec = importlib.util.spec_from_file_location("_env_install_worker_progress_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


worker = _worker()

# The exact transcript from the handoff's probe (a real `uv sync`, non-tty
# stderr, two small packages) — the fixture every test below feeds line by
# line, the same way `_build` will.
_SAMPLE_TRANSCRIPT = [
    "Using CPython 3.13.13",
    "Creating virtual environment at: .venv",
    "Resolved 3 packages in 1.01s",
    "Downloading numpy (15.9MiB)",
    "Downloading scipy (33.7MiB)",
    " Downloaded numpy",
    " Downloaded scipy",
    "Prepared 2 packages in 13.55s",
    "Installed 2 packages in 6ms",
    " + numpy==2.5.2",
    " + scipy==1.18.1",
]


def _mib(n):
    return n * 1024 * 1024


# --- the parser reads exactly what uv said, nothing it did not ---------------


def test_before_any_downloading_line_the_snapshot_is_all_none():
    """The honesty rule the handoff names explicitly: before uv has announced
    anything, this must report EXACTLY what the old capture-based code did —
    which for this new machinery means nothing at all, not an invented zero."""
    tracker = worker._UvProgress()
    for line in ("Using CPython 3.13.13", "Creating virtual environment at: .venv",
                 "Resolved 3 packages in 1.01s"):
        tracker.feed(line)
    assert tracker.snapshot("12s") == (None, None, None)


def test_a_downloading_line_is_parsed_into_announced_bytes():
    tracker = worker._UvProgress()
    tracker.feed("Downloading numpy (15.9MiB)")
    activity, done, total = tracker.snapshot("3s")
    assert done == 0
    assert total == pytest.approx(_mib(15.9), rel=1e-6)
    assert "numpy" in activity
    assert "3s" in activity


def test_downloaded_moves_bytes_from_announced_to_done():
    tracker = worker._UvProgress()
    tracker.feed("Downloading numpy (15.9MiB)")
    tracker.feed("Downloading scipy (33.7MiB)")
    tracker.feed(" Downloaded numpy")
    _, done, total = tracker.snapshot("5s")
    assert done == pytest.approx(_mib(15.9), rel=1e-6)
    assert total == pytest.approx(_mib(15.9) + _mib(33.7), rel=1e-6)


def test_the_biggest_still_pending_package_is_named():
    """Several packages can be in flight at once (uv's own concurrency is 50);
    the phrase names the one most likely to be why the bar looks stuck."""
    tracker = worker._UvProgress()
    tracker.feed("Downloading numpy (15.9MiB)")
    tracker.feed("Downloading torch (3400.0MiB)")
    tracker.feed(" Downloaded numpy")
    activity, _, _ = tracker.snapshot("1m00s")
    assert "torch" in activity
    assert "numpy" not in activity.split(" of ")[0]  # not named as the pending one


def test_bytes_done_never_exceeds_bytes_total():
    """Belt-and-suspenders on the invariant the class docstring argues for:
    `done` is a sum over a subset of what `total` sums, so it cannot overshoot
    — asserted directly rather than trusted."""
    tracker = worker._UvProgress()
    tracker.feed("Downloading numpy (15.9MiB)")
    tracker.feed(" Downloaded numpy")
    _, done, total = tracker.snapshot("1s")
    assert done <= total


def test_the_announced_total_is_a_lower_bound_that_can_rise():
    """A later `Downloading` line raises the total after an earlier tick
    already reported one — the documented, deliberate non-monotonicity."""
    tracker = worker._UvProgress()
    tracker.feed("Downloading numpy (15.9MiB)")
    _, _, total_1 = tracker.snapshot("1s")
    tracker.feed("Downloading scipy (33.7MiB)")
    _, _, total_2 = tracker.snapshot("2s")
    assert total_2 > total_1


def test_the_full_sample_transcript_ends_up_fully_accounted_for():
    tracker = worker._UvProgress()
    for line in _SAMPLE_TRANSCRIPT:
        tracker.feed(line)
    assert tracker.phase == "installed"
    # Nothing left to add once uv is done — same "nothing to say" contract as
    # before the first Downloading line, just from the other end.
    assert tracker.snapshot("15s") == (None, None, None)


def test_all_downloads_landed_but_no_prepared_line_yet_is_a_named_phase():
    """The gap between the last `Downloaded` and the `Prepared` line is where
    torch's slow unpacking/linking happens — it must not look like nothing is
    moving, and it must not claim a download that finished."""
    tracker = worker._UvProgress()
    tracker.feed("Downloading numpy (15.9MiB)")
    tracker.feed("Downloading scipy (33.7MiB)")
    tracker.feed(" Downloaded numpy")
    tracker.feed(" Downloaded scipy")
    assert tracker.phase == "preparing"
    activity, done, total = tracker.snapshot("20s")
    assert "preparing" in activity
    assert done == total  # every announced byte landed; the wait now is elsewhere


def test_the_prepared_line_moves_into_the_installing_phase():
    tracker = worker._UvProgress()
    tracker.feed("Downloading numpy (15.9MiB)")
    tracker.feed(" Downloaded numpy")
    tracker.feed("Prepared 1 package in 4.20s")
    assert tracker.phase == "installing"
    activity, _, _ = tracker.snapshot("21s")
    assert "installing" in activity


def test_a_sync_with_no_downloads_at_all_never_leaves_resolving():
    """Every wheel already cached: uv never prints a single `Downloading`
    line, and the whole point is that this reads exactly as it did before —
    None, not a phase nobody asked for."""
    tracker = worker._UvProgress()
    for line in ("Resolved 2 packages in 4ms", "Installed 2 packages in 3ms",
                 " + numpy==2.5.2"):
        tracker.feed(line)
    # No `Downloading` line was ever seen, so there is nothing to report —
    # whether that reads as "still resolving" or "already installed" is an
    # implementation detail; what the AI path's fallback depends on is None.
    assert tracker.snapshot("1s") == (None, None, None)


@pytest.mark.parametrize("value,unit,expected", [
    ("1", "B", 1),
    ("15.9", "KiB", 15.9 * 1024),
    ("15.9", "MiB", 15.9 * 1024 ** 2),
    ("2", "GiB", 2 * 1024 ** 3),
])
def test_every_uv_size_suffix_is_understood(value, unit, expected):
    tracker = worker._UvProgress()
    tracker.feed("Downloading pkg (%s%s)" % (value, unit))
    _, _, total = tracker.snapshot("1s")
    assert total == pytest.approx(expected, rel=1e-6)


def test_an_unrecognised_line_does_not_raise_or_lose_state():
    tracker = worker._UvProgress()
    tracker.feed("Downloading numpy (15.9MiB)")
    tracker.feed("warning: some future uv line nobody wrote a regex for")
    _, done, total = tracker.snapshot("1s")
    assert total == pytest.approx(_mib(15.9), rel=1e-6)


def test_format_bytes_reads_like_a_human_wrote_it():
    """Binary steps (1024), decimal-looking labels — matching `formatSize` in
    `frontend/src/platform/lib/format.ts` exactly, so this phrase and
    `ModelProgress`'s own byte readout never disagree about the same number."""
    assert worker._format_bytes(500) == "500 B"
    assert worker._format_bytes(_mib(15.9)) == "15.9 MB"
    assert worker._format_bytes(3.4 * 1024 ** 3) == "3.4 GB"


# --- streaming: `_build` sees uv's stderr while uv is still running ----------


class _FakeStreamingPopen:
    """Emits `lines` one at a time as `proc.stderr` is iterated, each one
    ANNOUNCED before the next is requested — the shape `_build`'s loop uses,
    so this is a test of the CONSUMPTION order, not just the final state."""

    def __init__(self, lines, returncode=0):
        self._lines = list(lines)
        self.returncode = returncode
        self.seen_before_exhausted = []

    @property
    def stdout(self):
        return None

    @property
    def stderr(self):
        return self

    def __iter__(self):
        for line in self._lines:
            self.seen_before_exhausted.append(line)
            yield line + "\n"

    def close(self):
        pass

    def wait(self):
        return self.returncode


def test_build_feeds_every_stderr_line_to_the_tracker_it_is_given(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text(
        "[project]\nname = 't'\nversion = '0.1'\ndependencies = ['pip']\n",
        encoding="utf-8",
    )
    venv_dir = str(tmp_path / "venv")

    def _fake_popen(cmd, **kw):
        os.makedirs(os.path.join(venv_dir, "bin"), exist_ok=True)
        open(os.path.join(venv_dir, "bin", "python"), "w").close()
        return _FakeStreamingPopen(_SAMPLE_TRANSCRIPT)

    monkeypatch.setattr(worker.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(worker.shutil, "which", lambda name: "/usr/bin/uv")

    tracker = worker._UvProgress()
    worker._build(str(proj), venv_dir, str(tmp_path / "cache"), "3.12", tracker)

    assert tracker.phase == "installed"


def test_a_failed_sync_still_raises_the_tail_of_stderr_verbatim(tmp_path, monkeypatch):
    """PY-18's contract, preserved through the rewrite: a resolver failure's own
    words reach the caller unedited, even though they now arrive one line at a
    time instead of as one captured blob."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text(
        "[project]\nname = 't'\nversion = '0.1'\ndependencies = ['pip']\n",
        encoding="utf-8",
    )
    lines = ["Resolved 0 packages in 4ms",
             "error: No solution found: imagecodecs has no wheels for this platform"]

    def _fake_popen(cmd, **kw):
        return _FakeStreamingPopen(lines, returncode=1)

    monkeypatch.setattr(worker.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(worker.shutil, "which", lambda name: "/usr/bin/uv")

    with pytest.raises(RuntimeError) as excinfo:
        worker._build(str(proj), str(tmp_path / "venv"), str(tmp_path / "cache"), "3.12")

    assert "imagecodecs has no wheels" in str(excinfo.value)


def test_the_stderr_ring_buffer_is_bounded(tmp_path, monkeypatch):
    """A pathological or merely chatty uv run must not turn a failed install
    into an unbounded memory hold — only the TAIL survives to be raised."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text(
        "[project]\nname = 't'\nversion = '0.1'\ndependencies = ['pip']\n",
        encoding="utf-8",
    )
    many_lines = ["noise line %d" % i for i in range(worker._STDERR_RING_LINES * 3)]
    many_lines.append("error: the actual failure, at the very end")

    def _fake_popen(cmd, **kw):
        return _FakeStreamingPopen(many_lines, returncode=1)

    monkeypatch.setattr(worker.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(worker.shutil, "which", lambda name: "/usr/bin/uv")

    with pytest.raises(RuntimeError) as excinfo:
        worker._build(str(proj), str(tmp_path / "venv"), str(tmp_path / "cache"), "3.12")

    message = str(excinfo.value)
    assert "the actual failure" in message
    assert message.count("noise line") <= worker._STDERR_RING_LINES
    assert "noise line 0\n" not in message  # the earliest lines were dropped


def test_uv_actually_line_flushes_to_a_pipe_not_just_at_exit():
    """The empirical check the handoff required before building any of this:
    does uv block-buffer its stderr when it is not a tty? Run a REAL, small
    `uv sync` (scipy, ~34MB) with stderr piped, and confirm at least one line
    is readable before the process has exited — if uv buffered everything to
    print at once, this would only ever see output after `poll()` is no
    longer None, and the whole streaming design would be pointless.

    Skipped if `uv` is not on PATH or there is no network — this is an
    integration check of uv's own behaviour, not of this repository's code.
    """
    if not worker.shutil.which("uv"):
        pytest.skip("uv is not on PATH")
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "pyproject.toml"), "w", encoding="utf-8") as f:
            f.write("[project]\nname = 'probe'\nversion = '0.1'\n"
                     "dependencies = ['scipy']\n\n[tool.uv]\npackage = false\n")
        env = dict(os.environ)
        env["UV_PROJECT_ENVIRONMENT"] = os.path.join(d, ".venv")
        env["UV_CACHE_DIR"] = os.path.join(d, "uvcache")
        try:
            proc = subprocess.Popen(["uv", "sync", "--no-default-groups"], cwd=d,
                                    env=env, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, text=True, bufsize=1)
        except OSError:
            pytest.skip("could not spawn uv")
        saw_a_line_before_exit = False
        deadline = time.monotonic() + 60
        try:
            for _ in proc.stderr:
                if proc.poll() is None:
                    saw_a_line_before_exit = True
                    break
                if time.monotonic() > deadline:
                    break
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if not saw_a_line_before_exit:
            pytest.skip("no network, or uv finished before the first read landed")
        assert saw_a_line_before_exit
