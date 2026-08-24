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
        #: every argument `mx.eval(...)` was called with, in order — how the
        #: eager-language-tower-eval fix (`load`'s own comment) is checked
        #: without a real MLX array to evaluate.
        self.evaled = []
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

    def eval(self, *args):
        with self._lock:
            self.evaled.append(args)


class _FakeTower:
    """A stand-in for `model.language_model` / `model.vision_tower`: nothing
    calls anything on it except `.parameters()`, which returns a MARKER
    (never real MLX arrays) so a test can tell which tower `mx.eval` touched
    without needing a real array to evaluate."""

    def __init__(self, marker):
        self._marker = marker

    def parameters(self):
        return self._marker


class _FakeVlmModel:
    """What `mlx_vlm.load` returns as the model half of its pair — minimal,
    with exactly the two attributes `worker.load`'s eager-eval fix reads:
    `language_model` (evaluated now, honest memory reporting) and
    `vision_tower` (left lazy, untouched by `load` at all)."""

    def __init__(self):
        self.language_model = _FakeTower("LANGUAGE_PARAMS")
        self.vision_tower = _FakeTower("VISION_PARAMS")


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
        return _FakeVlmModel(), "PROCESSOR"

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
    assert worker._loaded["processor"] == "PROCESSOR"
    assert worker._loaded["config"] == {"model_type": "qwen3_5"}
    assert isinstance(worker._loaded["model"], _FakeVlmModel)


# -- honest memory reporting: the LANGUAGE tower is evaluated at load time ----
# `lazy=True` alone (mlx_vlm.load's own default-off flag) leaves EVERYTHING
# unevaluated, language tower included — `memory()` (this module) would then
# report a freshly loaded, multi-gigabyte model as near-zero until the first
# generation forced the graph, and a load-time failure (corrupt weights, an
# OOM) would surface as a failed GENERATION instead of a failed LOAD, since
# `worker_base` marks the model "ready" the moment `load()` returns. So the
# language tower is evaluated NOW — exactly what mlx-lm always did, and
# exactly what mlx-vlm's own `lazy=False` does for BOTH towers
# (`mlx_vlm.utils.load_model`'s `if not lazy: mx.eval(model.parameters())`) --
# while the vision tower is left untouched, deferred to first use.
#
# Verified by hand on the real package: evaluating only `model.language_model`
# on `mlx-community/Qwen3.5-4B-OptiQ-4bit` lands at 3269.96 MB resident,
# against 3936.99 MB for evaluating both towers — matching mlx-lm's own
# 3.270GB figure almost exactly, with the 0.67GB difference being the vision
# tower this fix keeps lazy.


def test_load_evaluates_the_language_tower_but_leaves_the_vision_tower_lazy(
        monkeypatch):
    mlx_core = FakeMlxCore()
    _fake_mlx_vlm_with_config(monkeypatch)
    worker = load_worker(monkeypatch, mlx_core=mlx_core)

    worker.load("mlx-community/Qwen3.5-4B-OptiQ-4bit", "/snapshots/qwen")

    marks = [args[0] for args in mlx_core.evaled if args]
    assert "LANGUAGE_PARAMS" in marks, (
        "the language tower must be forced into memory at load time, not left "
        "for the first generation to discover a corrupt checkpoint")
    assert "VISION_PARAMS" not in marks, (
        "the vision tower must stay lazy — evaluating it here is the +0.67GB "
        "eager load this whole switch was built to avoid")


def test_load_evaluates_everything_for_an_architecture_with_no_language_model_attribute(
        monkeypatch):
    """Defensive fallback, not the expected path: every architecture mlx-vlm
    ships builds its `Model` around `self.language_model` (verified across
    qwen3, qwen3_5, gemma3, gemma4, llama and others), but a future one that
    broke that convention must not silently keep the honest-reporting bug --
    it should cost the +0.67GB-style eager load instead."""
    mlx_core = FakeMlxCore()
    mlx_vlm = types.ModuleType("mlx_vlm")
    plain_model = _FakeTower("ALL_PARAMS")  # no `.language_model` at all
    mlx_vlm.load = lambda path, lazy=False: (plain_model, _ProcessorWrappingATokenizer())
    utils = types.ModuleType("mlx_vlm.utils")
    utils.load_config = lambda path: {"model_type": "mystery"}
    monkeypatch.setitem(sys.modules, "mlx_vlm", mlx_vlm)
    monkeypatch.setitem(sys.modules, "mlx_vlm.utils", utils)
    worker = load_worker(monkeypatch, mlx_core=mlx_core)

    worker.load("org/mystery-architecture", "/snapshots/mystery")

    marks = [args[0] for args in mlx_core.evaled if args]
    assert "ALL_PARAMS" in marks



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
    worker._loaded.update(model=_FakeVlmModel(), processor=_ProcessorWrappingATokenizer(ids=(1, 2, 3)))

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
    mlx_vlm.load = lambda path, lazy=False: (_FakeVlmModel(), _ProcessorWrappingATokenizer())
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
    mlx_vlm.load = lambda path, lazy=False: (_FakeVlmModel(), _ProcessorWrappingATokenizer())
    mlx_vlm.stream_generate = lambda *a, **kw: iter(responses)
    sample_utils = types.ModuleType("mlx_vlm.sample_utils")
    sample_utils.make_sampler = lambda **kw: object()
    utils = types.ModuleType("mlx_vlm.utils")
    utils.load_config = lambda path: config if config is not None else {"model_type": "qwen3_5"}
    prompt_utils = types.ModuleType("mlx_vlm.prompt_utils")
    calls = []

    def _apply_chat_template(processor, config, messages, num_images=0, **kw):
        calls.append({"processor": processor, "config": config, "messages": messages,
                       "num_images": num_images, "kwargs": kw})
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


# -- an architecture mlx-vlm cannot open: named plainly, not in mlx-vlm's own
# words (AI-11j widened `visual-question-answering` to TEXT_GENERATION, and
# its canonical repo, `dandelin/vilt-b32-finetuned-vqa`, ships no
# `mlx_vlm.models.vilt` — this is what that load must say instead of mlx-vlm's
# own `ValueError: Model type vilt not supported. Error: …`)


def test_an_unopenable_architecture_fails_named_rather_than_in_mlx_vlms_own_words(
        worker, monkeypatch):
    """The failure this catches is real (mlx-vlm genuinely cannot open this
    checkpoint) — what changes is the WORDS, from a bare `ValueError` with no
    cause chain to walk, to a sentence that plainly names the architecture and
    says this is a fact about the checkpoint rather than the environment
    (`_mlx_load`'s own discipline for the identical problem: describe what is
    actually true, do not let a confusing message reach the page unexplained)."""
    _fake_mlx_vlm_with_config(monkeypatch, config={"model_type": "vilt"})
    utils = sys.modules["mlx_vlm.utils"]

    def _get_model_and_args(config):
        raise ValueError("Model type vilt not supported. Error: No module "
                         "named 'mlx_vlm.models.vilt'")

    utils.get_model_and_args = _get_model_and_args

    with pytest.raises(RuntimeError) as caught:
        worker.load("dandelin/vilt-b32-finetuned-vqa", "/snapshots/vilt")
    message = str(caught.value)

    assert "vilt" in message
    assert "dandelin/vilt-b32-finetuned-vqa" in message
    assert "fact about this checkpoint" in message
    # The chain survives, exactly as `_mlx_load`'s own wrapping keeps it —
    # a maintainer reading the log sees mlx-vlm's own words too.
    assert isinstance(caught.value.__cause__, ValueError)
    assert "not supported" in str(caught.value.__cause__)


def test_an_architecture_mlx_vlm_CAN_open_is_not_falsely_accused(worker, monkeypatch):
    """The opt-in check must not fire on the ordinary case: an architecture
    `get_model_and_args` resolves without complaint proceeds to load exactly
    as before this build."""
    _fake_mlx_vlm_with_config(monkeypatch, config={"model_type": "qwen3_5"})
    utils = sys.modules["mlx_vlm.utils"]
    utils.get_model_and_args = lambda config: (object(), config["model_type"])

    worker.load("mlx-community/Qwen3.5-4B-OptiQ-4bit", "/snapshots/qwen")

    assert worker._loaded["config"] == {"model_type": "qwen3_5"}


def test_a_generation_with_no_images_is_the_text_path_exactly_unchanged(worker, monkeypatch):
    """Empty/absent `images` must not touch `apply_chat_template` at all — the
    text path is `_messages_to_prompt` and nothing else, byte-identical to
    before this build."""

    class _Response:
        text = "hi"

    calls = _fake_mlx_vlm_with_config(monkeypatch, responses=[_Response()])
    worker._loaded.update(model=_FakeVlmModel(), processor=_ProcessorWrappingATokenizer(),
                          config={"model_type": "qwen3_5"})

    frames = []
    worker.generate({"messages": [{"role": "user", "content": "hi"}]}, frames.append)

    assert not calls, "the template helper must be untouched on the text-only path"
    assert frames[-1]["ok"] is True


# -- the MODEL axis: refuse when the LOADED checkpoint has no vision tower --
# The server's `_accepts_image` tries to prevent this by reading a cached
# `config.json`, but that is a PREDICTION and can disagree with reality (the
# model on disk swapped underneath, an older server with no gate at all, or
# a caller reaching this worker directly). The worker is the one place that
# knows what it actually loaded, so it asks its OWN copy of the config again.
#
# **Answered from CONFIG EVIDENCE (`vision_config`/`image_token_id`), never
# from `getattr(model, "vision_tower", None)`.** A first cut of this check
# read that one attribute name and was WRONG: verified against every
# `class Model(...)` in the installed mlx-vlm 0.6.15 package, at least 18
# real architectures name the identical thing `vision_model`, `vision` or
# `visual` instead of `vision_tower` — a getattr keyed on one spelling
# refused every genuine vision-language model spelled another way, which is
# the exact opposite of the intended behaviour and was invisible to a test
# that only faked the common name. `test_a_vision_model_under_an_uncommon_
# attribute_name_is_still_accepted` below is the case that would have caught
# it: same config evidence, a DIFFERENT attribute name, must still be
# accepted. The rule is refuse-on-POSITIVE-evidence-of-text-only, never on
# the absence of one recognised attribute — an architecture this file has
# never heard of must be let through when its config says it has a tower.


def test_images_are_refused_when_the_loaded_configs_own_evidence_says_text_only(
        worker, monkeypatch, tmp_path):
    """The server's `acceptsImage` flag is a prediction off a cached
    `config.json`; this is the worker's OWN check against the config it
    actually loaded, and it must fire whether or not the server's flag
    agreed. Neither `vision_config` nor `image_token_id` present — the
    genuinely text-only case."""
    photo = tmp_path / "cat.png"
    photo.write_bytes(b"not a real png, just bytes on disk")
    calls = _fake_mlx_vlm_with_config(monkeypatch)
    worker._loaded.update(model=_FakeVlmModel(),
                          processor=_ProcessorWrappingATokenizer(),
                          model_id="org/text-only-chat",
                          config={"model_type": "llama"})

    frames = []
    worker.generate(
        {"messages": [{"role": "user", "content": "what is this?"}],
         "images": [str(photo)]},
        frames.append)

    assert frames[-1]["ok"] is False
    assert "org/text-only-chat" in frames[-1]["error"]
    assert not calls, "apply_chat_template must never be reached for a tower-less model"


def test_a_vision_model_under_an_uncommon_attribute_name_is_still_accepted(
        worker, monkeypatch, tmp_path):
    """THE case a `getattr(model, "vision_tower", ...)` discriminator gets
    wrong: a real vision-language checkpoint whose config declares a tower
    (`image_token_id` here, the same evidence `hub_cache.has_vision_tower`
    reads) but whose mlx-vlm architecture module happens to expose that
    tower under `vision_model` rather than `vision_tower` — llama4,
    idefics2/3 and internvl_chat all do this in the installed 0.6.15
    package. Config evidence must accept it regardless of what the loaded
    object's attribute happens to be spelled."""
    photo = tmp_path / "cat.png"
    photo.write_bytes(b"not a real png, just bytes on disk")

    class _UncommonAttributeModel:
        """No `.vision_tower` at all — the tower lives at `.vision_model`
        instead, exactly like llama4/idefics2/idefics3/internvl_chat."""

        def __init__(self):
            self.language_model = _FakeTower("LANGUAGE_PARAMS")
            self.vision_model = _FakeTower("VISION_PARAMS_UNDER_A_DIFFERENT_NAME")

    class _Response:
        text = "a cat"

    calls = _fake_mlx_vlm_with_config(monkeypatch, responses=[_Response()])
    worker._loaded.update(model=_UncommonAttributeModel(),
                          processor=_ProcessorWrappingATokenizer(),
                          model_id="org/llama4-style-vlm",
                          config={"model_type": "llama4", "image_token_id": 128256})

    frames = []
    worker.generate(
        {"messages": [{"role": "user", "content": "what is this?"}],
         "images": [str(photo)]},
        frames.append)

    assert frames[-1]["ok"] is True, frames[-1]
    assert len(calls) == 1, "the config said this checkpoint has a tower — it must be used"


def test_the_vision_tower_check_fires_before_any_path_is_even_looked_at(
        worker, monkeypatch):
    """Order matters: a model that cannot use a picture at all should not
    make the caller wait on a filesystem check first."""
    calls = _fake_mlx_vlm_with_config(monkeypatch)
    worker._loaded.update(model=_FakeVlmModel(),
                          processor=_ProcessorWrappingATokenizer(),
                          model_id="org/text-only-chat",
                          config={"model_type": "llama"})

    frames = []
    worker.generate(
        {"messages": [{"role": "user", "content": "what is this?"}],
         "images": ["/this/path/does/not/exist/either.png"]},
        frames.append)

    assert frames[-1]["ok"] is False
    assert "org/text-only-chat" in frames[-1]["error"]
    # NOT the path-not-found message — the tower question is answered first.
    assert "does/not/exist" not in frames[-1]["error"]
    assert not calls


def test_an_image_bearing_request_uses_mlx_vlms_own_template_helper(worker, monkeypatch, tmp_path):
    """The ONE place `mlx_vlm.prompt_utils.apply_chat_template` is reached for
    — its image placeholder tokens are the point, and `num_images` must match
    the list's length exactly."""
    photo = tmp_path / "cat.png"
    photo.write_bytes(b"not a real png, just bytes on disk")
    # `image_token_id` is the config evidence the model-axis gate looks for
    # (alongside `vision_config`) — without it, this checkpoint would read as
    # text-only and never reach the template helper this test is about.
    vlm_config = {"model_type": "qwen3_5", "image_token_id": 151655}

    class _Response:
        text = "a cat"

    calls = _fake_mlx_vlm_with_config(monkeypatch, responses=[_Response()])
    worker._loaded.update(model=_FakeVlmModel(), processor=_ProcessorWrappingATokenizer(),
                          config=vlm_config)

    frames = []
    worker.generate(
        {"messages": [{"role": "user", "content": "what is this?"}], "images": [str(photo)]},
        frames.append)

    assert len(calls) == 1
    assert calls[0]["num_images"] == 1
    assert calls[0]["config"] == vlm_config
    # mlx-vlm's own `apply_chat_template` closes the think block by default on
    # any template that accepts `enable_thinking` (verified against the
    # installed 0.6.15 source) — this worker must override that, the same
    # thinking-stays-open behaviour the text path gets for free from the
    # tokenizer's own template (see `_messages_to_prompt`'s docstring).
    assert calls[0]["kwargs"].get("enable_thinking") is True
    assert frames[-1]["ok"] is True


def test_a_missing_image_path_names_the_path_rather_than_crashing_in_the_model(
        worker, monkeypatch, tmp_path):
    """A tensor-shape error out of the vision tower names no path at all — this
    is the check that stops a caller ever seeing one for a simple typo."""
    missing = str(tmp_path / "does-not-exist.png")
    calls = _fake_mlx_vlm_with_config(monkeypatch)
    # `image_token_id` clears the model-axis gate (this test is about the
    # PATH check that runs after it, not about a tower-less model).
    worker._loaded.update(model=_FakeVlmModel(), processor=_ProcessorWrappingATokenizer(),
                          config={"model_type": "qwen3_5", "image_token_id": 151655})

    frames = []
    worker.generate(
        {"messages": [{"role": "user", "content": "what is this?"}], "images": [missing]},
        frames.append)

    assert frames[-1]["ok"] is False
    assert missing in frames[-1]["error"]
    assert not calls, "a bad path must be caught before the model is ever asked"


def test_a_string_instead_of_a_list_is_not_iterated_as_characters(worker, monkeypatch):
    """The server validates `images`' shape today (`_images_problem`), but
    this worker's OWN contract must not depend on that — a single string
    handed in where a list was expected must read as "no images", not as one
    character-long "path" per character."""

    class _Response:
        text = "hi"

    calls = _fake_mlx_vlm_with_config(monkeypatch, responses=[_Response()])
    worker._loaded.update(model=_FakeVlmModel(), processor=_ProcessorWrappingATokenizer(),
                          config={"model_type": "qwen3_5"})

    frames = []
    worker.generate(
        {"messages": [{"role": "user", "content": "hi"}], "images": "/a/b.png"},
        frames.append)

    assert not calls, "a non-list 'images' must never reach the template helper"
    assert frames[-1]["ok"] is True


def test_an_empty_config_fails_named_rather_than_crashing_in_apply_chat_template(
        worker, monkeypatch, tmp_path):
    """`apply_chat_template` reads `config["model_type"]` unconditionally — an
    empty config must be caught here, with a named error, rather than let a
    bare KeyError reach the caller."""
    photo = tmp_path / "cat.png"
    photo.write_bytes(b"not a real png, just bytes on disk")
    calls = _fake_mlx_vlm_with_config(monkeypatch)
    worker._loaded.update(model=_FakeVlmModel(), processor=_ProcessorWrappingATokenizer(),
                          config={})

    frames = []
    worker.generate(
        {"messages": [{"role": "user", "content": "what is this?"}], "images": [str(photo)]},
        frames.append)

    assert frames[-1]["ok"] is False
    assert "configuration" in frames[-1]["error"]
    assert not calls, "apply_chat_template must never be reached with an empty config"


def test_input_tokens_is_none_on_the_image_path_rather_than_an_undercount(
        worker, monkeypatch, tmp_path):
    """The templated string counts ONE placeholder token per picture; the
    model reads far more once the vision tower expands it. Reporting the
    templated count under `input_tokens` would understate the real prompt
    cost by roughly the whole image — so this path reports nothing rather
    than a wrong number."""
    photo = tmp_path / "cat.png"
    photo.write_bytes(b"not a real png, just bytes on disk")

    class _Response:
        text = "a cat"

    _fake_mlx_vlm_with_config(monkeypatch, responses=[_Response()])
    # `image_token_id` clears the model-axis gate; this test is about the
    # DOWNSTREAM token count, not about whether the model has a tower.
    worker._loaded.update(model=_FakeVlmModel(),
                          processor=_ProcessorWrappingATokenizer(ids=(1, 2, 3)),
                          config={"model_type": "qwen3_5", "image_token_id": 151655})

    frames = []
    worker.generate(
        {"messages": [{"role": "user", "content": "what is this?"}], "images": [str(photo)]},
        frames.append)

    done = frames[-1]
    assert done["ok"] is True
    assert done["input_tokens"] is None


def test_an_mlx_without_thread_local_streams_is_left_alone(monkeypatch):
    """Streams were process-wide before 0.32 and there was nothing to pin. A
    runner that insisted on the newer call would turn a version skew into a
    worker that cannot generate at all."""
    # `eval` is a no-op here rather than absent: real old MLX has always had
    # `mx.eval`, and the absence this test is actually about is the
    # thread-local-stream calls, not this one.
    mlx_core = types.SimpleNamespace(cpu="CPU", gpu="GPU", eval=lambda *a: None)
    _fake_mlx_vlm(monkeypatch)
    worker = load_worker(monkeypatch, mlx_core=mlx_core)

    worker.load("mlx-community/Qwen3-8B-4bit", "/snapshots/qwen")
    frames = []
    worker.generate({"prompt": "hi"}, frames.append)

    assert frames[-1]["type"] == "done" and frames[-1]["ok"] is True, (
        "the runner must still generate with no pin")
