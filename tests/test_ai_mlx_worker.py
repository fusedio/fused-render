"""The MLX text runner's own behaviour — what is true of MLX and nothing else.

The contract half (routes, states, progress, the port handshake) is
`worker_base`'s and is covered by `tests/test_ai_worker_base.py`. What is left
here is what the runner decides for itself, and the first of those is what it
says when its own environment cannot answer.

Loaded by PATH with `worker_base` primed in `sys.modules`, exactly as
`tests/test_ai_whisper_worker.py` does: the runner finds its base off
`sys.path` in an interpreter of its own, so importing it the packaged way
(`fused_render.ai.runners.…`) would be testing an import that never ships.
"""
import importlib.util
import sys
import threading
import types
from pathlib import Path

import pytest

WORKER_PATH = str(
    Path(__file__).resolve().parents[1]
    / "fused_render" / "ai" / "runners" / "mlx_text" / "worker.py"
)


@pytest.fixture()
def worker(monkeypatch):
    base = types.ModuleType("worker_base")
    base.CANCEL = threading.Event()
    base.download_snapshot = lambda model_id, **kw: f"/snapshots/{model_id}"
    base.serve = lambda **kw: None

    monkeypatch.setitem(sys.modules, "worker_base", base)
    spec = importlib.util.spec_from_file_location("mlx_text_worker_under_test",
                                                  WORKER_PATH)
    assert spec is not None and spec.loader is not None, WORKER_PATH
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_an_unimportable_runner_environment_is_named_as_the_cause(worker, monkeypatch):
    """`Could not import module 'AutoTokenizer'` is not about the model.

    mlx-lm imports transformers for the tokenizer, so the import that fails is
    rarely the one named first — and what reached the AI Models page was that
    sentence, printed beside the name of a Qwen repo that was downloaded
    correctly and is not the problem. A user has no way to get from it to the
    thing that is actually broken.

    So this layer says only what it knows: mlx-lm did not import, out of THIS
    environment (`sys.prefix` — in a runner, this process is the venv the app
    built), with the original error kept. Diagnosing what that error means is
    `worker_base.describe_failure`'s job, one level up, because it is not
    specific to MLX.
    """
    # `None` in sys.modules is what makes an import raise ImportError on demand,
    # which is the same shape as a package whose files are half-written.
    monkeypatch.setitem(sys.modules, "mlx_lm", None)

    with pytest.raises(RuntimeError) as caught:
        worker.load("mlx-community/Qwen3-8B-4bit", "/snapshots/qwen")
    message = str(caught.value)

    assert sys.prefix in message, "the environment has to be named to be reported"
    assert "mlx-lm could not be imported" in message
    assert "rather than a problem with this model" in message
    assert "Qwen" not in message, "the model is not the subject"
    # And no invented cause: claiming an interrupted install sent a user to
    # delete an environment that had installed perfectly (the DMG's stdlib was
    # the real problem). What the failure MEANS is `worker_base`'s job.
    assert "interrupted" not in message
    assert "Delete" not in message


def test_the_import_error_itself_survives_into_the_message(worker, monkeypatch):
    """Wrapping must not swallow the original text: `AutoTokenizer` vs a missing
    `mlx` are the same class of failure with different repairs upstream, and the
    log is where that difference has to stay visible."""
    broken = types.ModuleType("mlx_lm")

    def _explode(name):
        raise ImportError("cannot import name 'AutoTokenizer' from 'transformers'")

    broken.__getattr__ = _explode
    monkeypatch.setitem(sys.modules, "mlx_lm", broken)

    with pytest.raises(RuntimeError) as caught:
        worker.load("mlx-community/Qwen3-8B-4bit", "/snapshots/qwen")

    assert "AutoTokenizer" in str(caught.value)
    assert isinstance(caught.value.__cause__, ImportError), (
        "the original traceback is what a maintainer reads; only the top-level "
        "message is ours"
    )


def test_a_working_environment_is_not_second_guessed(worker, monkeypatch):
    """The wrapper is an error path only: a real `mlx_lm.load` reaches the model
    untouched, and its own failures (a corrupt snapshot, an unsupported
    architecture) are not relabelled as an environment problem."""
    loaded = {}

    def _load(path):
        loaded["path"] = path
        return "MODEL", "TOKENIZER"

    fake = types.ModuleType("mlx_lm")
    fake.load = _load
    monkeypatch.setitem(sys.modules, "mlx_lm", fake)

    worker.load("mlx-community/Qwen3-8B-4bit", "/snapshots/qwen")

    assert loaded["path"] == "/snapshots/qwen"
    assert worker._loaded == {"model": "MODEL", "tokenizer": "TOKENIZER"}
