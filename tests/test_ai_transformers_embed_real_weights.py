"""A real-weights REPRODUCER for `runners/torch_embed.py`, run against
`google/siglip2-base-patch16-384` straight out of a local Hub cache.

**This file is NOT part of the default suite's coverage.** It is skipped
whenever `torch`/`transformers` are not importable or the checkpoint is not
already cached — which is EVERY run of this repo's own `.venv`, since those
packages live in a runner's own venv (built lazily on first Download) and are
never a dependency of the venv the rest of the suite runs under. The actual
guard against a `_pooled()`/`pooler_output` regression is `FakeOutput` in
`test_ai_transformers_embed_worker.py` — that file runs in every CI job and
every local `pytest` invocation; this one is a reproducer a developer runs
by hand, pointed at an interpreter that actually has torch, to confirm a real
checkpoint still behaves the way the fake assumes. Do not read a green run of
the default suite as evidence this file executed.

**Why it exists at all, alongside a fake.** `test_ai_transformers_embed_worker.py`
drives `generate()` through a hand-written `FakeModel`, and a fake can only
fail the assumptions its author wrote into it — an earlier version of that
fake encoded transformers 4.x's contract (`get_text_features` returning the
pooled tensor directly) rather than 5.x's (`BaseModelOutputWithPooling`, read
through `_pooled`), and the mocked suite stayed green while every real embed
call raised `AttributeError: 'BaseModelOutputWithPooling' object has no
attribute 'to'`. This file loads the real checkpoint through the real
`AutoModel`/`AutoProcessor` classes and asserts on vectors that came out the
other end, which a mock literally cannot do.

**Two ways to run it:**

* Bare `pytest`, no torch installed (the default here) — every test SKIPS,
  cheaply and offline. This is what CI sees.
* `FUSED_RENDER_REAL_WEIGHTS=1 pytest ...`, invoked with an interpreter that
  actually has torch/transformers (a runner's own venv — e.g.
  `~/.fused-render/venvs/<hash>/bin/python -m pytest
  tests/test_ai_transformers_embed_real_weights.py`) — an explicit opt-in
  that turns every skip condition into a hard FAILURE naming what is
  missing, so a typo'd venv path or an evicted cache entry cannot silently
  report "1 skipped" and be mistaken for a pass. A silent skip under an
  explicit opt-in would be the same trap this file exists to avoid, one
  level up.

**`google/siglip2-base-patch16-384` is a ~1.5GB download** (`catalog.py`'s own
figure for it) and this file must never fetch it — the skip/fail check is
keyed on the snapshot already existing under the ordinary Hub cache layout
(`~/.cache/huggingface/hub/models--<org>--<repo>/snapshots/*`), read directly
rather than through `huggingface_hub`, which this file has no other reason to
depend on.

**This runs on CPU, in fp32 — it is not a GPU dtype test.** `_placement()` in
`torch_embed.py` has no CUDA or ROCm hardware to place onto wherever this is
actually run, so it exercises the CONTRACT (the fields `_pooled` reads, the
shapes `generate()` returns) and the SEMANTICS (that the vectors it produces
actually cluster the way SigLIP2 is supposed to), not the accelerated
numerics `transformers-embed-cuda`/`-rocm` would need real hardware to check.
"""
import glob
import importlib.util
import os
import sys

import pytest

RUNNERS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "ai", "runners",
)
WORKER_PATH = os.path.join(RUNNERS, "torch_embed.py")

MODEL_ID = "google/siglip2-base-patch16-384"

#: Set to require this file to actually run rather than skip. See the module
#: docstring's "Two ways to run it" — unset (the default) is what every other
#: test in this repo runs under, and must stay fast and offline.
_REQUIRE_REAL_WEIGHTS = os.environ.get("FUSED_RENDER_REAL_WEIGHTS") == "1"

#: The ordinary Hub cache layout: `models--<org>--<repo>/snapshots/<rev>`.
#: Read directly rather than through `huggingface_hub` — this file must not
#: gain a dependency the rest of the repo's test suite does not already have,
#: and a glob over the cache directory is all `download()` would have handed
#: back anyway (`worker_base.download_snapshot` returns exactly this path).
_CACHE_ROOT = os.path.expanduser("~/.cache/huggingface/hub")
_SNAPSHOT_GLOB = os.path.join(
    _CACHE_ROOT, f"models--{MODEL_ID.replace('/', '--')}", "snapshots", "*")


def _real_snapshot():
    matches = sorted(glob.glob(_SNAPSHOT_GLOB))
    return matches[0] if matches else None


def _require_or_skip(condition, reason):
    """Skip on `condition` being false — unless `FUSED_RENDER_REAL_WEIGHTS=1`
    asked this file to actually run, in which case the same condition is a
    hard failure naming what is missing. One gate, two behaviours, so the
    opt-in cannot be satisfied by the thing it exists to catch: a quiet skip
    that reads like a pass.
    """
    if condition:
        return
    if _REQUIRE_REAL_WEIGHTS:
        pytest.fail(
            f"FUSED_RENDER_REAL_WEIGHTS=1 was set but {reason} — this run was "
            f"asked to actually exercise real weights, not skip past their "
            f"absence.", pytrace=False)
    pytest.skip(reason, allow_module_level=True)


try:
    import torch  # noqa: F401 - presence check only; see _require_or_skip below
    _HAVE_TORCH = True
except ImportError:
    _HAVE_TORCH = False

try:
    import transformers  # noqa: F401 - presence check only
    _HAVE_TRANSFORMERS = True
except ImportError:
    _HAVE_TRANSFORMERS = False

_require_or_skip(_HAVE_TORCH, "torch is not importable here — it lives in a "
                 "runner's own venv, not this interpreter")
_require_or_skip(_HAVE_TRANSFORMERS, "transformers is not importable here — "
                 "it lives in a runner's own venv, not this interpreter")

SNAPSHOT = _real_snapshot()
_require_or_skip(
    SNAPSHOT is not None,
    f"{MODEL_ID} is not in the local Hub cache ({_SNAPSHOT_GLOB}) — never "
    f"fetched here")


@pytest.fixture(scope="module")
def worker():
    """The real `torch_embed` module, with a real model loaded onto it.

    `worker_base` is stubbed exactly as the mocked suite stubs it — this file
    is about `torch_embed`'s own code, not about the download/report
    machinery `worker_base` already has its own tests for — but nothing else
    here is faked: `load()` runs unmodified against the real snapshot path.
    """
    import threading
    import types

    base = types.ModuleType("worker_base")
    base.CANCEL = threading.Event()
    base.download_snapshot = lambda model_id, **kw: SNAPSHOT
    base.serve = lambda **kw: None
    base.recorded = {}
    base.set_state = lambda **fields: base.recorded.update(fields)
    sys.modules["worker_base"] = base

    spec = importlib.util.spec_from_file_location(
        "torch_embed_real_weights_under_test", WORKER_PATH)
    assert spec is not None and spec.loader is not None, WORKER_PATH
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.worker_base = base
    module.load(MODEL_ID, module.download(MODEL_ID))
    yield module

    del sys.modules["worker_base"]


def _cos(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    return dot / (norm_a * norm_b)


def test_text_vectors_are_768_dim_and_unit_norm(worker):
    """SigLIP2 base's published width, and `unit_normalize`'s own promise."""
    result = worker.generate({"texts": ["a cat", "a dog", "a bicycle"]})
    assert result["dim"] == 768
    for row in result["vectors"]:
        assert len(row) == 768
        norm = sum(v * v for v in row) ** 0.5
        assert abs(norm - 1.0) < 1e-4


def test_semantically_closer_texts_cosine_higher(worker):
    """Two animals should read as closer than an animal and a vehicle.

    Tolerant bands around figures actually measured under
    `FUSED_RENDER_REAL_WEIGHTS=1` (torch 2.13.0+cpu, transformers 5.15.1):
    cos(cat, dog) = 0.9313, cos(cat, bicycle) = 0.8673. The assertion is the
    ORDERING plus a loose band, not the exact float — a transformers or torch
    point release reproducing the same relationship to three decimals is not
    a promise this test should make.
    """
    result = worker.generate({"texts": ["a cat", "a dog", "a bicycle"]})
    cat, dog, bicycle = result["vectors"]

    cos_cat_dog = _cos(cat, dog)
    cos_cat_bicycle = _cos(cat, bicycle)

    assert cos_cat_dog > cos_cat_bicycle
    assert 0.85 < cos_cat_dog < 0.99
    assert 0.55 < cos_cat_bicycle < 0.90
