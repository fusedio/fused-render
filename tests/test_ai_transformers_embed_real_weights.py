"""A real-weights smoke test for `runners/torch_embed.py`, run against
`google/siglip2-base-patch16-384` straight out of the local Hub cache.

**Why this exists alongside the mocked suite in
`test_ai_transformers_embed_worker.py`.** Every other test in that file drives
`generate()` through a `FakeModel` whose shape is asserted by hand — and an
earlier version of that fake encoded transformers 4.x's contract
(`get_text_features` returning the pooled tensor directly) rather than 5.x's
(`BaseModelOutputWithPooling`, read through `_pooled`). The suite was green
throughout, because a fake can only fail the assumptions its author wrote into
it. `worker._pooled` did not exist yet and every real embed call raised
`AttributeError: 'BaseModelOutputWithPooling' object has no attribute 'to'`.
This file is the guard a mock cannot be: it loads the real checkpoint through
the real `AutoModel`/`AutoProcessor` classes and asserts on vectors that came
out the other end, so a regression in `_pooled` — or in the padding rule, or
in `unit_normalize` — fails here even if a future fake is written carelessly
enough to hide it again.

**Skipped, never fetched.** `google/siglip2-base-patch16-384` is a ~1.5GB
download (`catalog.py`'s own figure for it), and CI has no Hub cache — asking
`from_pretrained` to go fetch it would make this file network-dependent and
slow on every machine that is not this one. The skip is keyed on the snapshot
already existing under the ordinary Hub cache layout
(`~/.cache/huggingface/hub/models--<org>--<repo>/snapshots/*`); `torch` and
`transformers` are also skipped-not-required, because they live in the
runner's own venv (built on first Download) and are not a dependency of the
repo's `.venv` that runs the rest of this suite.

**This runs on CPU here, in fp32 — it is not a GPU dtype test.** `_placement()`
in `torch_embed.py` has no CUDA or ROCm hardware to place onto on this
machine, so this file exercises the CONTRACT (the fields `_pooled` reads, the
shapes `generate()` returns) and the SEMANTICS (that the vectors it produces
actually cluster the way SigLIP2 is supposed to), not the accelerated
numerics `transformers-embed-cuda`/`-rocm` would need real hardware in CI to
check.
"""
import glob
import importlib.util
import os
import sys
from pathlib import Path

import pytest

RUNNERS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "ai", "runners",
)
WORKER_PATH = os.path.join(RUNNERS, "torch_embed.py")

MODEL_ID = "google/siglip2-base-patch16-384"

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


torch = pytest.importorskip("torch", reason="lives in the runner's own venv")
pytest.importorskip("transformers", reason="lives in the runner's own venv")

SNAPSHOT = _real_snapshot()
pytestmark = pytest.mark.skipif(
    SNAPSHOT is None,
    reason=f"{MODEL_ID} is not in the local Hub cache — never fetched here")


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

    Tolerant bands around figures measured on this machine after the
    `pooler_output` fix (commit 9ffcf768): cos(cat, dog) ~= 0.9423,
    cos(cat, bicycle) ~= 0.7573. The assertion is the ORDERING plus a loose
    band, not the exact float — a transformers or torch point release
    reproducing the same relationship to three decimals is not a promise this
    test should make.
    """
    result = worker.generate({"texts": ["a cat", "a dog", "a bicycle"]})
    cat, dog, bicycle = result["vectors"]

    cos_cat_dog = _cos(cat, dog)
    cos_cat_bicycle = _cos(cat, bicycle)

    assert cos_cat_dog > cos_cat_bicycle
    assert 0.85 < cos_cat_dog < 0.99
    assert 0.55 < cos_cat_bicycle < 0.90
