"""The machine-wide xdist worker budget in tests/conftest.py.

Many agent sessions run pytest at once on one Mac; `-n auto` in each meant
50+ workers on 10 cores. The hook hands each run a few flock-held slots.
"""
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pytest

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="flock slots are POSIX-only")


def _load_budget_conftest(config):
    """The module pytest loaded as tests/conftest.py.

    `import conftest` is ambiguous in a full run: the template test dirs ship
    their own conftest.py, all imported under the bare name `conftest`, so it
    binds to whichever loaded last. Ask the plugin manager instead.
    """
    here = Path(__file__).with_name("conftest.py")
    for plugin in config.pluginmanager.get_plugins():
        path = getattr(plugin, "__file__", None)
        if path and os.path.exists(path) and os.path.samefile(path, here):
            return plugin
    raise LookupError(f"{here} is not a registered pytest plugin")


@pytest.fixture
def budget_conftest(request):
    return _load_budget_conftest(request.config)


def _config(*args):
    return types.SimpleNamespace(args=list(args), option=types.SimpleNamespace(numprocesses="auto"))


@pytest.fixture(autouse=True)
def _fresh_budget(monkeypatch, tmp_path, budget_conftest):
    for var in ("CI", "PYTEST_XDIST_AUTO_NUM_WORKERS", "FUSED_RENDER_PYTEST_BUDGET",
                "FUSED_RENDER_PYTEST_MAX_WORKERS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    # The real session may hold slots (and a cached result) in this very module:
    # save them, run the test on a clean slate, then put them back untouched.
    saved_result = budget_conftest._XDIST_BUDGET_RESULT
    saved_info = budget_conftest._XDIST_BUDGET_INFO
    saved_slots = list(budget_conftest._HELD_XDIST_SLOTS)
    budget_conftest._XDIST_BUDGET_RESULT = None
    budget_conftest._XDIST_BUDGET_INFO = None
    budget_conftest._HELD_XDIST_SLOTS[:] = []
    try:
        yield
    finally:
        for fd in budget_conftest._HELD_XDIST_SLOTS:
            if fd not in saved_slots:
                os.close(fd)
        budget_conftest._XDIST_BUDGET_RESULT = saved_result
        budget_conftest._XDIST_BUDGET_INFO = saved_info
        budget_conftest._HELD_XDIST_SLOTS[:] = saved_slots


def _slot_dir(tmp_path):
    return tmp_path / "fused-render-pytest-slots"


def _locked_by_us(tmp_path):
    """Slot files we cannot lock ourselves from a fresh fd = held by the hook."""
    import fcntl
    n = 0
    for p in sorted(_slot_dir(tmp_path).glob("slot-*")):
        fd = os.open(p, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            n += 1
        finally:
            os.close(fd)
    return n


def test_explicit_env_override_defers_to_xdist(monkeypatch, budget_conftest):
    monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS", "2")
    assert budget_conftest.pytest_xdist_auto_num_workers(_config("tests")) is None


def test_ci_defers_to_xdist(monkeypatch, budget_conftest):
    monkeypatch.setenv("CI", "true")
    assert budget_conftest.pytest_xdist_auto_num_workers(_config("tests")) is None


def test_opt_out_defers_to_xdist(monkeypatch, budget_conftest):
    monkeypatch.setenv("FUSED_RENDER_PYTEST_BUDGET", "0")
    assert budget_conftest.pytest_xdist_auto_num_workers(_config("tests")) is None


def test_only_node_ids_runs_without_workers(budget_conftest):
    cfg = _config("tests/a.py::test_x", "tests/b.py::test_y")
    assert budget_conftest.pytest_xdist_auto_num_workers(cfg) == 0


@posix_only
def test_cap_is_respected(monkeypatch, tmp_path, budget_conftest):
    monkeypatch.setenv("FUSED_RENDER_PYTEST_MAX_WORKERS", "2")
    if (os.cpu_count() or 1) < 2:
        pytest.skip("needs 2 cpus")
    assert budget_conftest.pytest_xdist_auto_num_workers(_config("tests")) == 2
    assert _locked_by_us(tmp_path) == 2
    # idempotent: a second call does not take more slots
    assert budget_conftest.pytest_xdist_auto_num_workers(_config("tests")) == 2
    assert _locked_by_us(tmp_path) == 2
    assert "xdist budget: 2 workers" in budget_conftest.pytest_report_header(_config("tests"))[0]


@posix_only
def test_slots_held_by_another_process_leave_one_worker(monkeypatch, tmp_path, budget_conftest):
    total = os.cpu_count() or 1
    _slot_dir(tmp_path).mkdir()
    code = (
        "import fcntl, os, sys\n"
        f"fds = []\n"
        f"for i in range({total}):\n"
        f"    fd = os.open(os.path.join({str(_slot_dir(tmp_path))!r}, f'slot-{{i}}'), os.O_RDWR | os.O_CREAT, 0o644)\n"
        "    fcntl.flock(fd, fcntl.LOCK_EX); fds.append(fd)\n"
        "print('ready', flush=True)\n"
        "sys.stdin.read()\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "ready"
        assert budget_conftest.pytest_xdist_auto_num_workers(_config("tests")) == 1
        assert _locked_by_us(tmp_path) == total  # all held by the other process
    finally:
        proc.kill()
        proc.wait()
    # the kernel released the dead holder's flocks: slots are free again
    monkeypatch.setattr(budget_conftest, "_XDIST_BUDGET_RESULT", None)
    assert budget_conftest.pytest_xdist_auto_num_workers(_config("tests")) == min(4, total)
