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


class FakeMlxCore(types.ModuleType):
    """`mlx.core` as this runner uses it: two DEVICES and their STREAMS.

    The same double `tests/test_ai_mflux_worker.py` and
    `tests/test_ai_mlx_whisper_worker.py` keep, with the same two-device shape
    `mflux_image`'s copy needed: the default stream is per (thread, DEVICE)
    from mlx 0.32, so pinning only `default_device()` still aborts on the CPU
    half of the graph.
    """

    def __init__(self, **extra):
        super().__init__("mlx.core")
        self.cpu = "CPU"
        self.gpu = "GPU"
        #: the device of every `new_thread_unsafe_stream` call, in order.
        self.made = []
        #: (thread name, stream) for every `set_default_stream` call.
        self.pinned = []
        self._lock = threading.Lock()
        for name, value in extra.items():
            setattr(self, name, value)

    def default_device(self):
        return self.gpu

    def new_thread_unsafe_stream(self, device):
        with self._lock:
            self.made.append(device)
            return f"SHARED-{device}-STREAM"

    def set_default_stream(self, stream):
        with self._lock:
            self.pinned.append((threading.current_thread().name, stream))


def load_worker(monkeypatch, mlx_core=None):
    """A fresh import of the mlx-vlm worker, `worker_base` primed in
    `sys.modules` exactly as `tests/test_ai_mlx_whisper_worker.py`'s
    `load_worker` does — the runner finds its base off `sys.path` in an
    interpreter of its own, so importing it the packaged way
    (`fused_render.ai.runners.…`) would be testing an import that never ships.
    """
    base = types.ModuleType("worker_base")
    base.CANCEL = threading.Event()
    base.download_snapshot = lambda model_id, **kw: f"/snapshots/{model_id}"
    base.serve = lambda **kw: None
    monkeypatch.setitem(sys.modules, "worker_base", base)
    # `mlx.core` is no longer only a version-skew concern of `memory()`: `load`
    # and `generate` both pin this process's shared streams (`_pin_stream`), so
    # a test that left it out would be testing an import that cannot happen in
    # production. A caller may still hand in its own — an mlx too old to have
    # thread-local streams, say — and gets exactly that.
    if mlx_core is None:
        mlx_core = FakeMlxCore()
    mlx = types.ModuleType("mlx")
    mlx.core = mlx_core
    monkeypatch.setitem(sys.modules, "mlx", mlx)
    monkeypatch.setitem(sys.modules, "mlx.core", mlx_core)
    spec = importlib.util.spec_from_file_location("mlx_text_worker_under_test",
                                                  WORKER_PATH)
    assert spec is not None and spec.loader is not None, WORKER_PATH
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def worker(monkeypatch):
    return load_worker(monkeypatch)


def test_an_unimportable_runner_environment_is_named_as_the_cause(worker, monkeypatch):
    """`Could not import module 'AutoTokenizer'` is not about the model.

    mlx-vlm imports transformers for the processor, so the import that fails
    is rarely the one named first — and what reached the AI Models page was
    that sentence, printed beside the name of a Qwen repo that was downloaded
    correctly and is not the problem. A user has no way to get from it to the
    thing that is actually broken.

    So this layer says only what it knows: mlx-vlm did not import, out of THIS
    environment (`sys.prefix` — in a runner, this process is the venv the app
    built), with the original error kept. Diagnosing what that error means is
    `worker_base.describe_failure`'s job, one level up, because it is not
    specific to MLX.
    """
    # `None` in sys.modules is what makes an import raise ImportError on demand,
    # which is the same shape as a package whose files are half-written.
    monkeypatch.setitem(sys.modules, "mlx_vlm", None)

    with pytest.raises(RuntimeError) as caught:
        worker.load("mlx-community/Qwen3-8B-4bit", "/snapshots/qwen")
    message = str(caught.value)

    assert sys.prefix in message, "the environment has to be named to be reported"
    assert "mlx-vlm could not be imported" in message
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
    broken = types.ModuleType("mlx_vlm")

    def _explode(name):
        raise ImportError("cannot import name 'AutoTokenizer' from 'transformers'")

    broken.__getattr__ = _explode
    monkeypatch.setitem(sys.modules, "mlx_vlm", broken)

    with pytest.raises(RuntimeError) as caught:
        worker.load("mlx-community/Qwen3-8B-4bit", "/snapshots/qwen")

    assert "AutoTokenizer" in str(caught.value)
    assert isinstance(caught.value.__cause__, ImportError), (
        "the original traceback is what a maintainer reads; only the top-level "
        "message is ours"
    )


def test_a_working_environment_is_not_second_guessed(worker, monkeypatch):
    """The wrapper is an error path only: a real `mlx_vlm.load` reaches the
    model untouched, and its own failures (a corrupt snapshot, an unsupported
    architecture) are not relabelled as an environment problem."""
    loaded = {}

    def _load(path, lazy=False):
        loaded["path"] = path
        loaded["lazy"] = lazy
        return "MODEL", "PROCESSOR"

    fake = types.ModuleType("mlx_vlm")
    fake.load = _load
    utils = types.ModuleType("mlx_vlm.utils")
    utils.load_config = lambda path: {"model_type": "qwen3_5"}
    monkeypatch.setitem(sys.modules, "mlx_vlm", fake)
    monkeypatch.setitem(sys.modules, "mlx_vlm.utils", utils)

    worker.load("mlx-community/Qwen3-8B-4bit", "/snapshots/qwen")

    assert loaded["path"] == "/snapshots/qwen"
    # The measured trade this switch is built on (worker.py's own comment):
    # eager loading materialises the vision tower a text-only chat never
    # touches (+0.67GB on Qwen3.5-4B), and `lazy=True` defers it to first use.
    assert loaded["lazy"] is True
    assert worker._loaded == {"model": "MODEL", "processor": "PROCESSOR",
                              "config": {"model_type": "qwen3_5"}}


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


class _ProcessorWithOwnEncode:
    """The rare processor that exposes `encode` itself — must be preferred
    over `.tokenizer.encode` when both exist, since it is the more specific
    answer."""

    def __init__(self, ids=(1, 2, 3, 4)):
        self._ids = list(ids)
        # A `.tokenizer` that would answer differently, so a test using this
        # class can tell WHICH one `_prompt_tokens` actually read.
        self.tokenizer = _Tokenizer(ids=(99,))

    def encode(self, text):
        return self._ids


class _ProcessorWrappingATokenizer:
    """The ordinary shape: mlx-vlm's `load()` returns a `ProcessorMixin`, which
    wraps a tokenizer at `.tokenizer` and does not expose `encode` itself."""

    def __init__(self, ids=(1, 2, 3, 4), raises=False):
        self.tokenizer = _Tokenizer(ids=ids, raises=raises)


def test_the_prompt_is_counted_off_the_processors_own_encode_when_it_has_one(worker):
    assert worker._prompt_tokens(_ProcessorWithOwnEncode(ids=(5, 6, 7)), "hello") == 3


def test_the_prompt_is_counted_off_the_wrapped_tokenizer_when_the_processor_has_none(worker):
    """mlx-vlm's processor does not usually implement `encode` itself — it
    wraps a tokenizer at `.tokenizer`, and that is where the count comes from
    once the processor itself has nothing to offer."""
    assert worker._prompt_tokens(_ProcessorWrappingATokenizer(ids=(5, 6, 7)), "hello") == 3


def test_a_tokenizer_that_cannot_count_costs_the_metric_not_the_completion(worker):
    """None means "not reported", which every reader already handles — a
    generation must not fail because a counter did."""
    assert worker._prompt_tokens(_ProcessorWrappingATokenizer(raises=True), "hello") is None
    assert worker._prompt_tokens(object(), "hello") is None


def test_both_terminal_frames_carry_the_prompt_count(worker, monkeypatch):
    """Including the CANCELLED one: the prompt was read whether or not the
    answer was still wanted by the end."""

    class _Response:
        text = "hi"

    mlx_vlm = types.ModuleType("mlx_vlm")
    mlx_vlm.stream_generate = lambda *a, **kw: iter([_Response(), _Response()])
    sample_utils = types.ModuleType("mlx_vlm.sample_utils")
    sample_utils.make_sampler = lambda **kw: object()
    monkeypatch.setitem(sys.modules, "mlx_vlm", mlx_vlm)
    monkeypatch.setitem(sys.modules, "mlx_vlm.sample_utils", sample_utils)
    worker._loaded.update(model=object(), processor=_ProcessorWrappingATokenizer(ids=(1, 2, 3)))

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


# -- the MLX stream pin, shared with mlx_whisper and mflux_image --------------


def _fake_mlx_vlm(monkeypatch, responses=()):
    mlx_vlm = types.ModuleType("mlx_vlm")
    mlx_vlm.load = lambda path, lazy=False: ("MODEL", _ProcessorWrappingATokenizer())
    mlx_vlm.stream_generate = lambda *a, **kw: iter(responses)
    sample_utils = types.ModuleType("mlx_vlm.sample_utils")
    sample_utils.make_sampler = lambda **kw: object()
    utils = types.ModuleType("mlx_vlm.utils")
    utils.load_config = lambda path: {"model_type": "qwen3_5"}
    monkeypatch.setitem(sys.modules, "mlx_vlm", mlx_vlm)
    monkeypatch.setitem(sys.modules, "mlx_vlm.sample_utils", sample_utils)
    monkeypatch.setitem(sys.modules, "mlx_vlm.utils", utils)


def test_the_load_and_the_generate_share_ONE_mlx_stream_PER_DEVICE(monkeypatch):
    """From mlx 0.32 the default stream belongs to the THREAD that made it, and
    an unevaluated array forced anywhere else throws
    `std::runtime_error("There is no Stream(gpu, 1) in current thread")` out of
    `metal::get_command_encoder` — an UNCAUGHT C++ exception, the same one
    `mlx_whisper` and `mflux_image` already had to design around. `load` runs
    on `worker_base.serve`'s bring-up thread, which then exits, and `generate`
    arrives on a fresh `ThreadingTCPServer` request thread — precisely the
    shape that trips it, and precisely why switching models (which respawns
    this worker as a brand-new process) reproduces the crash on the very next
    message: that message is the first time any thread besides the bring-up
    thread touches the new model's weights.
    """
    mlx_core = FakeMlxCore()
    _fake_mlx_vlm(monkeypatch)
    worker = load_worker(monkeypatch, mlx_core=mlx_core)

    loader = threading.Thread(
        target=worker.load, args=("mlx-community/Qwen3-8B-4bit", "/snapshots/qwen"),
        name="bring-up")
    loader.start()
    loader.join()
    request = threading.Thread(
        target=worker.generate, args=({"prompt": "hi"}, lambda _f: None),
        name="request-1")
    request.start()
    request.join()

    threads = {name for name, _stream in mlx_core.pinned}
    streams = {stream for _name, stream in mlx_core.pinned}
    assert len(threads) > 1, f"only one thread pinned a stream: {mlx_core.pinned}"
    assert streams == {"SHARED-CPU-STREAM", "SHARED-GPU-STREAM"}, mlx_core.pinned
    # One stream per device for the whole process, not one per thread: a second
    # would be a second owner, which is the thing being prevented.
    assert sorted(mlx_core.made) == ["CPU", "GPU"], mlx_core.made


# -- the image path (commit 3): a list of absolute paths on the CURRENT turn --


def _fake_mlx_vlm_with_config(monkeypatch, config=None, responses=()):
    """`_fake_mlx_vlm`, plus the two extra modules the image path reaches for:
    `mlx_vlm.utils.load_config` (read once, at load time) and
    `mlx_vlm.prompt_utils.apply_chat_template` (the ONE place mlx-vlm's own
    template helper is correct — see `worker._messages_to_prompt`)."""
    mlx_vlm = types.ModuleType("mlx_vlm")
    mlx_vlm.load = lambda path, lazy=False: ("MODEL", _ProcessorWrappingATokenizer())
    mlx_vlm.stream_generate = lambda *a, **kw: iter(responses)
    sample_utils = types.ModuleType("mlx_vlm.sample_utils")
    sample_utils.make_sampler = lambda **kw: object()
    utils = types.ModuleType("mlx_vlm.utils")
    utils.load_config = lambda path: config if config is not None else {"model_type": "qwen3_5"}
    prompt_utils = types.ModuleType("mlx_vlm.prompt_utils")
    calls = []

    def _apply_chat_template(processor, config, messages, num_images=0, **kw):
        calls.append({"processor": processor, "config": config, "messages": messages,
                       "num_images": num_images})
        return "TEMPLATED-PROMPT-WITH-IMAGE-TOKENS"

    prompt_utils.apply_chat_template = _apply_chat_template
    monkeypatch.setitem(sys.modules, "mlx_vlm", mlx_vlm)
    monkeypatch.setitem(sys.modules, "mlx_vlm.sample_utils", sample_utils)
    monkeypatch.setitem(sys.modules, "mlx_vlm.utils", utils)
    monkeypatch.setitem(sys.modules, "mlx_vlm.prompt_utils", prompt_utils)
    return calls


def test_load_stashes_the_config_for_the_image_path_to_reuse(worker, monkeypatch):
    """Read ONCE at load time, not per request — `generate`'s image path needs
    `model_type` off this dict for mlx-vlm's own template helper, and a request
    is not where a filesystem read this load already paid for belongs."""
    _fake_mlx_vlm_with_config(monkeypatch, config={"model_type": "gemma4"})

    worker.load("mlx-community/gemma-4-4bit", "/snapshots/gemma")

    assert worker._loaded["config"] == {"model_type": "gemma4"}


def test_a_generation_with_no_images_is_the_text_path_exactly_unchanged(worker, monkeypatch):
    """Empty/absent `images` must not touch `apply_chat_template` at all — the
    text path is `_messages_to_prompt` and nothing else, byte-identical to
    before this build."""

    class _Response:
        text = "hi"

    calls = _fake_mlx_vlm_with_config(monkeypatch, responses=[_Response()])
    worker._loaded.update(model=object(), processor=_ProcessorWrappingATokenizer(),
                          config={"model_type": "qwen3_5"})

    frames = []
    worker.generate({"messages": [{"role": "user", "content": "hi"}]}, frames.append)

    assert not calls, "the template helper must be untouched on the text-only path"
    assert frames[-1]["ok"] is True


def test_an_image_bearing_request_uses_mlx_vlms_own_template_helper(worker, monkeypatch, tmp_path):
    """The ONE place `mlx_vlm.prompt_utils.apply_chat_template` is reached for
    — its image placeholder tokens are the point, and `num_images` must match
    the list's length exactly."""
    photo = tmp_path / "cat.png"
    photo.write_bytes(b"not a real png, just bytes on disk")

    class _Response:
        text = "a cat"

    calls = _fake_mlx_vlm_with_config(monkeypatch, responses=[_Response()])
    worker._loaded.update(model=object(), processor=_ProcessorWrappingATokenizer(),
                          config={"model_type": "qwen3_5"})

    frames = []
    worker.generate(
        {"messages": [{"role": "user", "content": "what is this?"}], "images": [str(photo)]},
        frames.append)

    assert len(calls) == 1
    assert calls[0]["num_images"] == 1
    assert calls[0]["config"] == {"model_type": "qwen3_5"}
    assert frames[-1]["ok"] is True


def test_a_missing_image_path_names_the_path_rather_than_crashing_in_the_model(
        worker, monkeypatch, tmp_path):
    """A tensor-shape error out of the vision tower names no path at all — this
    is the check that stops a caller ever seeing one for a simple typo."""
    missing = str(tmp_path / "does-not-exist.png")
    calls = _fake_mlx_vlm_with_config(monkeypatch)
    worker._loaded.update(model=object(), processor=_ProcessorWrappingATokenizer(),
                          config={"model_type": "qwen3_5"})

    frames = []
    worker.generate(
        {"messages": [{"role": "user", "content": "what is this?"}], "images": [missing]},
        frames.append)

    assert frames[-1]["ok"] is False
    assert missing in frames[-1]["error"]
    assert not calls, "a bad path must be caught before the model is ever asked"


def test_an_mlx_without_thread_local_streams_is_left_alone(monkeypatch):
    """Streams were process-wide before 0.32 and there was nothing to pin. A
    runner that insisted on the newer call would turn a version skew into a
    worker that cannot generate at all."""
    mlx_core = types.SimpleNamespace(cpu="CPU", gpu="GPU")
    _fake_mlx_vlm(monkeypatch)
    worker = load_worker(monkeypatch, mlx_core=mlx_core)

    worker.load("mlx-community/Qwen3-8B-4bit", "/snapshots/qwen")
    frames = []
    worker.generate({"prompt": "hi"}, frames.append)

    assert frames[-1]["type"] == "done" and frames[-1]["ok"] is True, (
        "the runner must still generate with no pin")
