"""`supervisor._spawn`'s ROCm HSA preload wiring (see `hsa_preload.py`):

* `_child_env` prepends a resolved preload onto `LD_PRELOAD` without
  clobbering an operator's own value.
* `_spawn` asks `hsa_preload.resolve_preload` for a path, hands it to the
  spawn, and — if and only if a preloaded attempt fails to come up — retries
  ONCE with the preload removed, so a bad system runtime cannot brick every
  ROCm worker on the machine.

These are unit tests of the orchestration (does the right value reach the
right place, does the retry fire under the right conditions), not
integration tests of an actual worker process — `test_ai_runtime.py` already
owns the real spawn-and-wait machinery via its FAKE_WORKER fixture, and nothing
here needs a live subprocess to prove.
"""
import pytest

from fused_render.ai import hsa_preload, registry, supervisor


def test_child_env_prepends_the_preload():
    env = supervisor._child_env("t", preload="/opt/rocm/lib/libhsa-runtime64.so.1")
    assert env["LD_PRELOAD"] == "/opt/rocm/lib/libhsa-runtime64.so.1"


def test_child_env_preserves_an_inherited_preload(monkeypatch):
    monkeypatch.setenv("LD_PRELOAD", "/usr/lib/libmalloc-profiler.so")
    env = supervisor._child_env("t", preload="/opt/rocm/lib/libhsa-runtime64.so.1")
    assert env["LD_PRELOAD"] == (
        "/opt/rocm/lib/libhsa-runtime64.so.1:/usr/lib/libmalloc-profiler.so"
    )


def test_child_env_with_no_preload_leaves_ld_preload_untouched(monkeypatch):
    monkeypatch.setenv("LD_PRELOAD", "/usr/lib/libmalloc-profiler.so")
    env = supervisor._child_env("t")
    assert env["LD_PRELOAD"] == "/usr/lib/libmalloc-profiler.so"


def test_child_env_adds_nothing_when_there_is_no_inherited_value_either(monkeypatch):
    monkeypatch.delenv("LD_PRELOAD", raising=False)
    env = supervisor._child_env("t")
    assert "LD_PRELOAD" not in env


def _runner(tmp_path):
    return registry.Runner(
        code="fake", capability=registry.TEXT_GENERATION, folder=str(tmp_path),
        label="Fake",
    )


def test_spawn_resolves_and_passes_the_preload_through(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(hsa_preload, "resolve_preload", lambda python: "/opt/rocm/lib/libhsa-runtime64.so.1")
    monkeypatch.setattr(supervisor, "_spawn_once", lambda runner, worker, python, preload: calls.append(preload))

    worker = supervisor.Worker(model="m", capability=registry.TEXT_GENERATION, runner_code="fake")
    supervisor._spawn(_runner(tmp_path), worker, "/venv/bin/python")

    assert calls == ["/opt/rocm/lib/libhsa-runtime64.so.1"]


def test_spawn_passes_none_through_when_resolver_says_no(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(hsa_preload, "resolve_preload", lambda python: None)
    monkeypatch.setattr(supervisor, "_spawn_once", lambda runner, worker, python, preload: calls.append(preload))

    worker = supervisor.Worker(model="m", capability=registry.TEXT_GENERATION, runner_code="fake")
    supervisor._spawn(_runner(tmp_path), worker, "/venv/bin/python")

    assert calls == [None]


def test_a_preloaded_bring_up_failure_is_retried_once_without_it(monkeypatch, tmp_path):
    calls = []

    def fake_spawn_once(runner, worker, python, preload):
        calls.append(preload)
        if preload:
            raise supervisor.SupervisorError("the worker exited before it started (code 127)")

    monkeypatch.setattr(hsa_preload, "resolve_preload", lambda python: "/opt/rocm/lib/libhsa-runtime64.so.1")
    monkeypatch.setattr(supervisor, "_spawn_once", fake_spawn_once)

    worker = supervisor.Worker(model="m", capability=registry.TEXT_GENERATION, runner_code="fake")
    supervisor._spawn(_runner(tmp_path), worker, "/venv/bin/python")

    assert calls == ["/opt/rocm/lib/libhsa-runtime64.so.1", None]


def test_a_second_failure_without_preload_is_not_retried_again(monkeypatch, tmp_path):
    """The retry is ONE attempt, not a loop — a worker that can't come up for
    a reason that has nothing to do with the preload must still fail, not
    spin."""
    calls = []

    def fake_spawn_once(runner, worker, python, preload):
        calls.append(preload)
        raise supervisor.SupervisorError("the worker exited before it started (code 1)")

    monkeypatch.setattr(hsa_preload, "resolve_preload", lambda python: "/opt/rocm/lib/libhsa-runtime64.so.1")
    monkeypatch.setattr(supervisor, "_spawn_once", fake_spawn_once)

    worker = supervisor.Worker(model="m", capability=registry.TEXT_GENERATION, runner_code="fake")
    with pytest.raises(supervisor.SupervisorError):
        supervisor._spawn(_runner(tmp_path), worker, "/venv/bin/python")

    assert calls == ["/opt/rocm/lib/libhsa-runtime64.so.1", None]


def test_a_failure_with_no_preload_in_play_is_never_retried(monkeypatch, tmp_path):
    """No preload was ever attempted (a CUDA/CPU/MLX worker, or a ROCm one the
    resolver declined), so there is nothing to fall back FROM — a bring-up
    failure here is just a failure."""
    calls = []

    def fake_spawn_once(runner, worker, python, preload):
        calls.append(preload)
        raise supervisor.SupervisorError("the worker never published a port")

    monkeypatch.setattr(hsa_preload, "resolve_preload", lambda python: None)
    monkeypatch.setattr(supervisor, "_spawn_once", fake_spawn_once)

    worker = supervisor.Worker(model="m", capability=registry.TEXT_GENERATION, runner_code="fake")
    with pytest.raises(supervisor.SupervisorError):
        supervisor._spawn(_runner(tmp_path), worker, "/venv/bin/python")

    assert calls == [None]


def test_a_cancel_during_a_preloaded_attempt_is_never_retried(monkeypatch, tmp_path):
    """`worker.stopping` racing the bootstrap wait raises `SupervisorError
    ("cancelled")` — a user-initiated stop, not a preload problem, and
    re-spawning a worker nobody wants any more would be its own bug."""
    calls = []

    def fake_spawn_once(runner, worker, python, preload):
        calls.append(preload)
        raise supervisor.SupervisorError("cancelled")

    monkeypatch.setattr(hsa_preload, "resolve_preload", lambda python: "/opt/rocm/lib/libhsa-runtime64.so.1")
    monkeypatch.setattr(supervisor, "_spawn_once", fake_spawn_once)

    worker = supervisor.Worker(model="m", capability=registry.TEXT_GENERATION, runner_code="fake")
    with pytest.raises(supervisor.SupervisorError, match="cancelled"):
        supervisor._spawn(_runner(tmp_path), worker, "/venv/bin/python")

    assert calls == ["/opt/rocm/lib/libhsa-runtime64.so.1"]
