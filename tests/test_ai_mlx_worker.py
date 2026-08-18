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


# -- what the prompt cost ------------------------------------------------------
# `input_tokens` (SPEC AI-3) is counted HERE because this process holds the
# tokenizer that decides the answer; anything upstream could only estimate it,
# and an estimate under the same label as the model's own number is worse than
# no number at all.


class _Tokenizer:
    def __init__(self, ids=(1, 2, 3, 4), raises=False):
        self._ids = list(ids)
        self._raises = raises

    def encode(self, text):
        if self._raises:
            raise ValueError("this tokenizer refuses that string")
        return self._ids


def test_the_prompt_is_counted_in_the_models_own_tokens(worker):
    assert worker._prompt_tokens(_Tokenizer(ids=(5, 6, 7)), "hello") == 3


def test_a_tokenizer_that_cannot_count_costs_the_metric_not_the_completion(worker):
    """None means "not reported", which every reader already handles — a
    generation must not fail because a counter did."""
    assert worker._prompt_tokens(_Tokenizer(raises=True), "hello") is None
    assert worker._prompt_tokens(object(), "hello") is None


def test_both_terminal_frames_carry_the_prompt_count(worker, monkeypatch):
    """Including the CANCELLED one: the prompt was read whether or not the
    answer was still wanted by the end."""
    import types

    class _Response:
        text = "hi"

    mlx_lm = types.ModuleType("mlx_lm")
    mlx_lm.stream_generate = lambda *a, **kw: iter([_Response(), _Response()])
    sample_utils = types.ModuleType("mlx_lm.sample_utils")
    sample_utils.make_sampler = lambda **kw: object()
    monkeypatch.setitem(sys.modules, "mlx_lm", mlx_lm)
    monkeypatch.setitem(sys.modules, "mlx_lm.sample_utils", sample_utils)
    worker._loaded.update(model=object(), tokenizer=_Tokenizer(ids=(1, 2, 3)))

    frames = []
    worker.generate({"prompt": "hello"}, frames.append)
    done = frames[-1]
    assert done["type"] == "done" and done["input_tokens"] == 3
    assert done["tokens"] == 2

    frames.clear()
    worker_base = sys.modules["worker_base"]
    worker_base.CANCEL.set()
    try:
        worker.generate({"prompt": "hello"}, frames.append)
    finally:
        worker_base.CANCEL.clear()
    assert frames[-1]["cancelled"] is True
    assert frames[-1]["input_tokens"] == 3
