"""A real-weights PARITY GATE for `runners/onnx_embed.py`: the ONNX runner's
vectors against transformers' own, on `siglip2-base-patch16-384`, straight out
of a local Hub cache.

**This is the evidence that licenses deleting the torch embedding runner.** The
argument for ONNX Runtime is entirely about the environment — tens of megabytes
against up to 5.9 GB of torch wheel — and it only holds if the vectors are the
same vectors. A page that indexed a folder of photos on one engine and searches
it from the other must get sensible answers, and no mocked test can establish
that: `tests/test_ai_onnx_embed_worker.py` proves the runner reads the right
tensor out of a graph it was handed, not that the graph computes what SigLIP2
computes. So this file loads both engines' real weights and compares.

**It is NOT part of the default suite's coverage.** It is skipped whenever
`onnxruntime`/`tokenizers`/`torch`/`transformers` are not all importable or
either snapshot is not already cached — which is EVERY run of this repo's own
`.venv`, since those packages live in runner venvs (built lazily on first
Download) and are never a dependency of the venv the rest of the suite runs
under. Do not read a green run of the default suite as evidence this file
executed.

**The torch side goes through `AutoModel` directly, not through
`runners/torch_embed.py`.** That is deliberate and it is what lets this file
outlive the runner it is a gate on: Stage 2 deletes `torch_embed.py`, and a
parity test that imported it would have to be deleted with it — leaving the
claim it exists to support unverifiable from that commit onwards. Reproducing
the four lines that matter here (`AutoProcessor`, `padding="max_length"`,
`get_text_features`, `pooler_output`) pins the ONNX runner against
TRANSFORMERS' contract rather than against our wrapper of it, which is the
stronger statement anyway.

**Three ways to run it:**

* Bare `pytest`, nothing installed (the default here) — every test SKIPS,
  cheaply and offline. This is what CI sees.
* `FUSED_RENDER_REAL_WEIGHTS=1 pytest ...`, invoked with an interpreter that
  has all four packages — an explicit opt-in that turns every skip condition
  into a hard FAILURE naming what is missing, so a typo'd venv path or an
  evicted cache entry cannot silently report "N skipped" and be mistaken for a
  pass. A silent skip under an explicit opt-in would be the same trap this file
  exists to avoid, one level up.
* The ONNX runner's own venv alone (`~/.fused-render/venvs/<hash>/bin/python -m
  pytest tests/test_ai_onnx_embed_real_weights.py`) runs the ONNX-only tests —
  the dimensions, the semantics, the fetched-bytes gate — and skips the two
  parity ones. Useful, and not a substitute: the parity pair is the point.

  A parity venv is built by hand, because no runner folder declares both
  families and none should:

      uv venv /tmp/parity && \\
        uv pip install --python /tmp/parity/bin/python \\
          onnxruntime tokenizers torch transformers pillow

**Neither snapshot is ever fetched by this file.** Between them they are ~3 GB
(`catalog.py`'s own figures), and the skip/fail check is keyed on the snapshots
already existing under the ordinary Hub cache layout
(`~/.cache/huggingface/hub/models--<org>--<repo>/snapshots/*`), read directly
rather than through `huggingface_hub`, which this file has no other reason to
depend on.

**This runs on the CPU provider, in fp32 — it is not a GPU test.** The
DirectML, CUDA and ROCm rows have no hardware here to place onto, so this
exercises the CONTRACT (the outputs `_output_index` reads, the shapes
`generate()` returns) and the NUMERICS (that they match torch's), not the
accelerated kernels those three rows would need real hardware to check.
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
WORKER_PATH = os.path.join(RUNNERS, "onnx_embed.py")

MODEL_ID = "onnx-community/siglip2-base-patch16-384-ONNX"
#: The checkpoint the export above was converted FROM — loaded through
#: transformers for the parity comparison. Not a curated model any more once
#: Stage 2 lands; it stays named here because a parity claim needs both halves.
TORCH_MODEL_ID = "google/siglip2-base-patch16-384"

#: `catalog.py`'s `size_gb` for `MODEL_ID`, restated rather than imported.
#:
#: Restated because this file may be run by a RUNNER's interpreter, which has no
#: `fused_render` on its path at all — and cross-checked against the catalog in
#: `test_the_size_figure_here_matches_the_catalogs` below whenever the import
#: does work, so the two cannot drift apart silently.
EXPECTED_FETCHED_GB = 1.5

#: How far the on-disk total may sit from the figure above. One rounding step:
#: the catalog carries one decimal, so anything within 0.05 GB is the same
#: number. Wide enough for a re-export that moved a few megabytes, nowhere near
#: wide enough to hide a second copy of either tower (the smallest is 187 MB).
FETCHED_TOLERANCE_GB = 0.05

#: Set to require this file to actually run rather than skip. See the module
#: docstring's "Three ways to run it" — unset (the default) is what every other
#: test in this repo runs under, and must stay fast and offline.
_REQUIRE_REAL_WEIGHTS = os.environ.get("FUSED_RENDER_REAL_WEIGHTS") == "1"

_CACHE_ROOT = os.path.expanduser("~/.cache/huggingface/hub")


def _snapshot_glob(model_id):
    """The ordinary Hub cache layout: `models--<org>--<repo>/snapshots/<rev>`.

    Read directly rather than through `huggingface_hub` — this file must not
    gain a dependency the rest of the repo's test suite does not already have,
    and a glob over the cache directory is all `download()` would have handed
    back anyway (`worker_base.download_snapshot` returns exactly this path).
    """
    return os.path.join(_CACHE_ROOT, f"models--{model_id.replace('/', '--')}",
                        "snapshots", "*")


def _real_snapshot(model_id):
    matches = sorted(glob.glob(_snapshot_glob(model_id)))
    return matches[0] if matches else None


def _require_or_skip(condition, reason):
    """Skip on `condition` being false — unless `FUSED_RENDER_REAL_WEIGHTS=1`
    asked this file to actually run, in which case the same condition is a hard
    failure naming what is missing. One gate, two behaviours, so the opt-in
    cannot be satisfied by the thing it exists to catch: a quiet skip that reads
    like a pass. Copied from `test_ai_transformers_embed_real_weights.py`, whose
    docstring argues it at length.
    """
    if condition:
        return
    if _REQUIRE_REAL_WEIGHTS:
        pytest.fail(
            f"FUSED_RENDER_REAL_WEIGHTS=1 was set but {reason} — this run was "
            f"asked to actually exercise real weights, not skip past their "
            f"absence.", pytrace=False)
    pytest.skip(reason, allow_module_level=True)


def _importable(name):
    try:
        __import__(name)
        return True
    except ImportError:
        return False


_HAVE_ONNXRUNTIME = _importable("onnxruntime")
_HAVE_TOKENIZERS = _importable("tokenizers")
_HAVE_TORCH = _importable("torch")
_HAVE_TRANSFORMERS = _importable("transformers")

_require_or_skip(_HAVE_ONNXRUNTIME, "onnxruntime is not importable here — it "
                 "lives in a runner's own venv, not this interpreter")
_require_or_skip(_HAVE_TOKENIZERS, "tokenizers is not importable here — it "
                 "lives in a runner's own venv, not this interpreter")

SNAPSHOT = _real_snapshot(MODEL_ID)
_require_or_skip(
    SNAPSHOT is not None,
    f"{MODEL_ID} is not in the local Hub cache ({_snapshot_glob(MODEL_ID)}) — "
    f"never fetched here")

TORCH_SNAPSHOT = _real_snapshot(TORCH_MODEL_ID)

#: The probe set, and it is FIXED rather than generated: a parity number is only
#: comparable between runs if both engines saw the same strings. Deliberately
#: mixed — two near-synonyms, an unrelated noun, a multilingual pair and a
#: sentence long enough to exercise the padding rule — because a wrong
#: `padding=` shifts short strings least and would hide behind a probe set of
#: single words.
PROBE_TEXTS = [
    "a cat",
    "a dog",
    "a bicycle",
    "une bicyclette",
    "a photograph of a small animal asleep on a sofa in the afternoon sun",
]


@pytest.fixture(scope="module")
def worker():
    """The real `onnx_embed` module, with the real sessions loaded onto it.

    `worker_base` is stubbed exactly as the mocked suite stubs it — this file is
    about `onnx_embed`'s own code, not about the download/report machinery
    `worker_base` already has its own tests for — and its `download_snapshot`
    returns the CACHED path, so nothing here can reach the network. Everything
    else runs unmodified: `load()` opens the real graphs, the real tokenizer and
    the real `preprocessor_config.json`.
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
        "onnx_embed_real_weights_under_test", WORKER_PATH)
    assert spec is not None and spec.loader is not None, WORKER_PATH
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.worker_base = base
    module.load(MODEL_ID, SNAPSHOT)
    yield module

    del sys.modules["worker_base"]


@pytest.fixture(scope="module")
def probe_image(tmp_path_factory):
    """One deterministic image, written here rather than shipped.

    A generated gradient rather than a photograph: both engines see the identical
    pixels, which is all a parity comparison needs, and a checked-in JPEG would
    be a binary in the repo for one assertion. 512px so the 384px resize actually
    runs rather than being a no-op.
    """
    from PIL import Image

    path = tmp_path_factory.mktemp("probe") / "gradient.png"
    image = Image.new("RGB", (512, 512))
    pixels = image.load()
    for x in range(512):
        for y in range(512):
            pixels[x, y] = (x // 2, y // 2, (x + y) // 4)
    image.save(path)
    return str(path)


@pytest.fixture(scope="module")
def torch_vectors(probe_image):
    """The same probes through `transformers`, as `(texts, images)`.

    Four lines of transformers and they are the four that matter — see the
    module docstring on why this does not go through `runners/torch_embed.py`.
    `padding="max_length"` and `pooler_output` are reproduced verbatim from that
    runner's own `_TEXT_PADDING` and `_pooled`, because a parity test that
    quietly used a different padding or a different output field would compare
    two things neither engine actually computes.
    """
    _require_or_skip(_HAVE_TORCH, "torch is not importable here — the parity "
                     "half needs an interpreter with both engines (see the "
                     "module docstring)")
    _require_or_skip(_HAVE_TRANSFORMERS, "transformers is not importable here "
                     "— the parity half needs an interpreter with both engines")
    _require_or_skip(
        TORCH_SNAPSHOT is not None,
        f"{TORCH_MODEL_ID} is not in the local Hub cache "
        f"({_snapshot_glob(TORCH_MODEL_ID)}) — the parity half compares against "
        f"it and this file never fetches")

    import torch
    from PIL import Image
    from transformers import AutoModel, AutoProcessor

    model = AutoModel.from_pretrained(TORCH_SNAPSHOT)
    model.eval()
    processor = AutoProcessor.from_pretrained(TORCH_SNAPSHOT)

    with torch.inference_mode():
        inputs = processor(text=PROBE_TEXTS, padding="max_length",
                          truncation=True, return_tensors="pt")
        texts = model.get_text_features(**inputs).pooler_output
        image_inputs = processor(images=[Image.open(probe_image).convert("RGB")],
                                return_tensors="pt")
        images = model.get_image_features(**image_inputs).pooler_output

    return (_unit(texts.to(dtype=torch.float32).tolist()),
            _unit(images.to(dtype=torch.float32).tolist()))


def _unit(rows):
    """`embed_common.unit_normalize`, restated — the ONNX side gets it applied
    inside `generate()`, so the torch side has to have it too or the cosines
    would be comparing a normalized vector to an unnormalized one."""
    out = []
    for row in rows:
        norm = sum(v * v for v in row) ** 0.5
        out.append([v / norm for v in row] if norm > 0 else list(row))
    return out


def _cos(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    return dot / (norm_a * norm_b)


# -- the ONNX runner on its own -------------------------------------------------


def test_text_vectors_are_768_dim_and_unit_norm(worker):
    """SigLIP2 base's published width, and `unit_normalize`'s own promise."""
    result = worker.generate({"texts": PROBE_TEXTS})
    assert result["dim"] == 768
    assert len(result["vectors"]) == len(PROBE_TEXTS)
    for row in result["vectors"]:
        assert len(row) == 768
        norm = sum(v * v for v in row) ** 0.5
        assert abs(norm - 1.0) < 1e-4


def test_image_vectors_are_768_dim_and_unit_norm(worker, probe_image):
    """The vision tower's own width, which for SigLIP2 is the same 768 — the
    towers project into ONE space, which is the whole point of a dual encoder
    and the reason a text query can rank images at all."""
    result = worker.generate({"paths": [probe_image]})
    assert result["dim"] == 768
    norm = sum(v * v for v in result["vectors"][0]) ** 0.5
    assert abs(norm - 1.0) < 1e-4


def test_semantically_closer_texts_cosine_higher(worker):
    """Two animals should read as closer than an animal and a vehicle.

    The same assertion `test_ai_transformers_embed_real_weights.py` makes about
    the torch runner, restated here so the ONNX runner's own semantics are
    pinned even in a venv with no torch at all: the ORDERING plus a loose band,
    never the exact float — a point release reproducing the same relationship to
    three decimals is not a promise this test should make.
    """
    result = worker.generate({"texts": PROBE_TEXTS})
    cat, dog, bicycle = result["vectors"][0], result["vectors"][1], result["vectors"][2]

    cos_cat_dog = _cos(cat, dog)
    cos_cat_bicycle = _cos(cat, bicycle)

    assert cos_cat_dog > cos_cat_bicycle
    assert 0.85 < cos_cat_dog < 0.99
    assert 0.55 < cos_cat_bicycle < 0.90


def test_the_multilingual_pair_lands_close(worker):
    """"a bicycle" and "une bicyclette" — SigLIP2's multilingual training is
    the reason this export is curated over an English-only encoder, and it is
    also a second, independent check that the tokenizer configured here is the
    one the checkpoint was trained with: a wrong normalizer breaks the French
    string first."""
    result = worker.generate({"texts": PROBE_TEXTS})
    assert _cos(result["vectors"][2], result["vectors"][3]) > 0.8


# -- the parity gate, which is what licenses Stage 2 ----------------------------


def test_the_text_towers_agree_with_transformers(worker, torch_vectors):
    """≥0.999 cosine, per probe, against transformers' own vectors.

    Not equality: an ONNX graph and a torch module reassociate the same fp32
    arithmetic differently, so the last few digits legitimately differ. 0.999 is
    tight enough that a wrong padding, a wrong output tensor or a wrong
    normalizer all fail it — each of those moves a cosine by 0.01 or more, not
    by 0.0001.
    """
    onnx_rows = worker.generate({"texts": PROBE_TEXTS})["vectors"]
    torch_rows = torch_vectors[0]
    assert len(onnx_rows) == len(torch_rows) == len(PROBE_TEXTS)
    for text, onnx_row, torch_row in zip(PROBE_TEXTS, onnx_rows, torch_rows):
        assert _cos(onnx_row, torch_row) >= 0.999, text


def test_the_vision_towers_agree_with_transformers(worker, torch_vectors,
                                                   probe_image):
    """The other tower, and it needs its own assertion rather than trusting the
    text one: `_preprocess_images` reimplements a transformers image processor by
    hand (resize, rescale, normalize) with no library to check it against, so
    this is the ONLY test that can catch a wrong `image_mean` or a wrong
    resample filter — both of which produce a perfectly well-shaped vector that
    means something else."""
    onnx_row = worker.generate({"paths": [probe_image]})["vectors"][0]
    assert _cos(onnx_row, torch_vectors[1][0]) >= 0.999


def test_a_text_and_an_image_vector_are_comparable_across_engines(
        worker, torch_vectors, probe_image):
    """The promise a page actually relies on: index photos on one engine, search
    them from the other. Asserted as a CROSS product — this engine's image
    vector against the other engine's text vectors — because two towers agreeing
    tower-by-tower would still permit a shared rotation that broke the joint
    space."""
    onnx_image = worker.generate({"paths": [probe_image]})["vectors"][0]
    for text, torch_text in zip(PROBE_TEXTS, torch_vectors[0]):
        own = _cos(onnx_image,
                   worker.generate({"texts": [text]})["vectors"][0])
        crossed = _cos(onnx_image, torch_text)
        assert abs(own - crossed) < 0.005, text


# -- the download's scope, which is the 11.42 GB gate ---------------------------


def test_the_cached_snapshot_is_the_fp32_set_and_not_the_whole_repo():
    """A widened `allow_patterns` must not be able to reintroduce the full pull.

    This repo publishes eight quantizations of each tower side by side — 33
    files, 11.42 GB. `runners/onnx_embed.py`'s `download()` pins the fp32
    graphs, and the ONLY place that pin can be checked against reality is a
    machine where the download actually happened. Measured over the snapshot
    tree with `os.walk` and `st_size`, following the symlinks the Hub cache
    stores (each snapshot entry points at a blob), so this is the bytes the
    fetch really cost.
    """
    total = 0
    for dirpath, _dirnames, filenames in os.walk(SNAPSHOT):
        for name in filenames:
            total += os.stat(os.path.join(dirpath, name)).st_size
    gigabytes = total / 1e9
    assert abs(gigabytes - EXPECTED_FETCHED_GB) < FETCHED_TOLERANCE_GB, (
        f"{MODEL_ID}'s cached snapshot is {gigabytes:.2f} GB, and catalog.py "
        f"prices it at {EXPECTED_FETCHED_GB} GB. If it is much larger, a "
        f"widened allow_patterns has started fetching the quantized copies "
        f"(the whole repo is 11.42 GB); if much smaller, the fp32 graphs or an "
        f"external-data sidecar are missing and the session should not have "
        f"opened at all.")


def test_no_quantized_graph_is_on_the_disk():
    """The same gate stated as a per-FILE rule, because the byte total alone
    would pass on a snapshot that had swapped fp32 for int8 rather than added to
    it — and that swap is the one change that would keep the download small and
    void the parity numbers above. fp32 everywhere is not an optimization
    choice: it is what makes ≥0.999 achievable."""
    names = []
    for _dirpath, _dirnames, filenames in os.walk(SNAPSHOT):
        names.extend(filenames)
    for name in names:
        for tag in ("fp16", "int8", "uint8", "q4", "bnb4", "quantized"):
            assert tag not in name, (
                f"{name} is in the cached snapshot — this runner fetches fp32 "
                f"only (see `onnx_embed.download`), and a quantized graph here "
                f"means the pattern list has drifted.")
    # …and the fp32 pair really is present, so the assertion above cannot pass
    # vacuously on an empty or half-written snapshot.
    assert "text_model.onnx" in names
    assert "vision_model.onnx" in names


def test_the_size_figure_here_matches_the_catalogs():
    """`EXPECTED_FETCHED_GB` is restated in this file rather than imported (a
    runner's interpreter has no `fused_render` on its path), so the two are
    cross-checked wherever the import DOES work. A restated constant nothing
    compares is a constant that drifts."""
    try:
        from fused_render.ai import catalog
    except ImportError:  # pragma: no cover - a runner venv, which is the point
        pytest.skip("fused_render is not importable from this interpreter")
    entry = next(e for e in catalog.SUGGESTIONS["onnx-embed"]
                 if e["id"] == MODEL_ID)
    assert entry["size_gb"] == EXPECTED_FETCHED_GB
