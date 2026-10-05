"""Every runner's `download()` fetches what its engine opens, and not the other
formats a Hub repo publishes beside it.

Hub repos routinely carry one set of weights in several formats —
`pytorch_model.bin` beside `model.safetensors`, `original/consolidated.00.pth`,
`flax_model.msgpack`, `tf_model.h5`, an `onnx/` export — and a bare
`download_snapshot(model_id)` fetches all of them. The scopes live in
`formats.py` (one place; every runner's interpreter imports it) and these tests
pin both halves: the constants say what they should against a fixture listing,
and each runner's `download` actually passes them.

The listing is a FIXTURE and `selects` is the real predicate, so a pattern that
looks right but does not `fnmatch` the way `huggingface_hub` does fails here.
"""
import importlib.util
import os
import sys
import threading
import types

import pytest

from fused_render.ai.runners import formats, worker_base

RUNNERS = os.path.join(os.path.dirname(formats.__file__))


class _FakeBase(types.ModuleType):
    """Just enough `worker_base` for a worker module to import and `download`."""

    class Cancelled(Exception):
        pass

    def __init__(self):
        super().__init__("worker_base")
        self.CANCEL = threading.Event()
        self.calls = []
        self.selects = worker_base.selects

    def download_snapshot(self, model_id, allow_patterns=None,
                          ignore_patterns=None, **kwargs):
        self.calls.append((model_id, allow_patterns, ignore_patterns))
        return f"/snapshots/{model_id}"

    def download_plan(self, phases):
        return [self.download_snapshot(m, allow_patterns=a, ignore_patterns=i)
                for m, a, i in phases]

    def download_file(self, *args, **kwargs):
        return "/snapshots/file"

    def serve(self, **kwargs):
        return None

    def __getattr__(self, name):
        # Anything else a worker touches at import time is a no-op.
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *a, **kw: None


def _worker(monkeypatch, folder):
    base = _FakeBase()
    monkeypatch.setitem(sys.modules, "worker_base", base)
    path = os.path.join(RUNNERS, folder, "worker.py")
    spec = importlib.util.spec_from_file_location(f"{folder}_scopes_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, base


def _kept(names, allow=None, ignore=None):
    return sorted(n for n in names
                  if worker_base.selects(n, allow=allow, ignore=ignore))


#: A representative transformers-style repo that also carries every other format.
MULTI_FORMAT = [
    "config.json", "generation_config.json", "tokenizer.json",
    "tokenizer_config.json", "chat_template.jinja", "preprocessor_config.json",
    "special_tokens_map.json", "model.safetensors",
    "model-00001-of-00002.safetensors", "model.safetensors.index.json",
    "pytorch_model.bin", "pytorch_model-00001-of-00002.bin",
    "pytorch_model.bin.index.json", "training_args.bin", "optimizer.pt",
    "flax_model.msgpack", "tf_model.h5", "rust_model.ot", "model.onnx",
    "model.onnx_data", "model-q4.gguf",
    "onnx/model.onnx", "onnx/model_fp16.onnx", "openvino/openvino_model.bin",
    "original/consolidated.00.pth", "original/params.json",
    "original/tokenizer.model", "README.md", ".gitattributes",
    "custom_code.py",
]

#: What an MLX engine opens out of that listing — and, by the same token,
#: everything it may need that a closed allowlist would have dropped.
MLX_KEEPS = [
    ".gitattributes", "README.md", "chat_template.jinja", "config.json",
    "custom_code.py", "generation_config.json",
    "model-00001-of-00002.safetensors", "model.safetensors",
    "model.safetensors.index.json", "preprocessor_config.json",
    "pytorch_model.bin.index.json", "special_tokens_map.json",
    "tokenizer.json", "tokenizer_config.json",
]


def test_mlx_ignore_drops_every_format_an_mlx_engine_cannot_open():
    kept = _kept(MULTI_FORMAT, ignore=list(formats.MLX_IGNORE))
    assert kept == MLX_KEEPS


def test_mlx_ignore_never_touches_a_safetensors_file_or_a_json_config():
    names = ["model.safetensors", "weights.safetensors", "transformer/a.safetensors",
             "text_encoder/model-00001-of-00002.safetensors", "config.json",
             "vae/config.json", "tokenizer/vocab.json", "tokenizer.model",
             "model.safetensors.index.json", "split_model.json"]
    assert _kept(names, ignore=list(formats.MLX_IGNORE)) == sorted(names)


def test_ct2_files_are_faster_whispers_own_allow_list_and_keep_model_bin():
    names = ["config.json", "preprocessor_config.json", "model.bin",
             "tokenizer.json", "vocabulary.json", "vocabulary.txt",
             "pytorch_model.bin", "model.safetensors", "README.md",
             "flax_model.msgpack", "tf_model.h5", "onnx/model.onnx",
             ".gitattributes"]
    assert _kept(names, allow=list(formats.CT2_FILES)) == [
        "config.json", "model.bin", "preprocessor_config.json",
        "tokenizer.json", "vocabulary.json", "vocabulary.txt"]


def test_mlx_whisper_files_cover_all_three_weight_spellings_and_nothing_else():
    names = ["config.json", "weights.npz", "weights.safetensors",
             "model.safetensors", "pytorch_model.bin", "README.md",
             "tokenizer.json", ".gitattributes"]
    assert _kept(names, allow=list(formats.MLX_WHISPER_FILES)) == [
        "config.json", "model.safetensors", "weights.npz", "weights.safetensors"]


def test_diffusers_ignore_drops_foreign_exports_but_keeps_torch_weights():
    names = ["model_index.json", "unet/config.json",
             "unet/diffusion_pytorch_model.safetensors",
             "unet/diffusion_pytorch_model.bin",
             "unet/diffusion_pytorch_model.fp16.safetensors",
             "text_encoder/model.safetensors",
             "text_encoder/pytorch_model.bin",
             "unet/flax_model.msgpack", "text_encoder/tf_model.h5",
             "unet/model.onnx", "onnx/unet/model.onnx",
             "unet/model.onnx_data", "openvino/x.xml", "text_encoder/rust_model.ot"]
    kept = _kept(names, ignore=list(formats.DIFFUSERS_IGNORE))
    assert kept == sorted([
        "model_index.json", "unet/config.json",
        "unet/diffusion_pytorch_model.safetensors",
        "unet/diffusion_pytorch_model.bin",
        "unet/diffusion_pytorch_model.fp16.safetensors",
        "text_encoder/model.safetensors", "text_encoder/pytorch_model.bin"])


# ---------------------------------------------------------------- each runner


@pytest.mark.parametrize("folder", ["mlx_text", "mlx_embed", "mflux_image",
                                    "laya_mlx", "mlx_audio_tts"])
def test_mlx_runners_ignore_the_unreadable_formats(monkeypatch, folder):
    worker, base = _worker(monkeypatch, folder)
    worker.download("u/x")
    assert base.calls == [("u/x", None, list(formats.MLX_IGNORE))]


def test_mlx_whisper_fetches_only_its_closed_file_set(monkeypatch):
    worker, base = _worker(monkeypatch, "mlx_whisper")
    worker.download("u/x")
    assert base.calls[0] == ("u/x", list(formats.MLX_WHISPER_FILES), None)


def test_faster_whisper_fetches_only_its_closed_file_set(monkeypatch):
    worker, base = _worker(monkeypatch, "faster_whisper")
    worker.download("u/x")
    assert base.calls == [("u/x", list(formats.CT2_FILES), None)]


def test_ltx_gemma_phase_ignores_the_unreadable_formats(monkeypatch):
    worker, base = _worker(monkeypatch, "ltx_video")
    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "list_repo_files", lambda repo: [
        "transformer-distilled.safetensors", "config.json"])
    worker.download("u/ltx")
    gemma = [c for c in base.calls if c[0] == worker._GEMMA_MODEL_ID]
    assert gemma == [(worker._GEMMA_MODEL_ID, None, list(formats.MLX_IGNORE))]


def test_a_noncurated_diffusers_repo_ignores_foreign_exports(monkeypatch):
    from fused_render.ai.runners import torch_image

    calls = []
    monkeypatch.setattr(
        torch_image.worker_base, "download_snapshot",
        lambda model_id, **kw: calls.append((model_id, kw)) or "/snap")
    result = torch_image.download("u/not-curated")
    assert calls == [("u/not-curated",
                      {"ignore_patterns": list(formats.DIFFUSERS_IGNORE)})]
    assert result == {"snapshot": "/snap", "gguf": None}


# ------------------------------------------------- engines already at the minimum


def test_every_auxiliary_component_is_ONE_file_fetched_with_download_file():
    """silero-vad, pyannote segmentation, the speaker embedding and the GGUF FLUX
    transformer are each a single named file: `vad.py`/`diarize.py` call
    `download_file(repo, FILE)`, never `download_snapshot`, so there is no repo
    to over-fetch. Pinned by the table having a `file` per repo and by neither
    module mentioning a snapshot fetch."""
    from fused_render.ai.runners import diarize, vad

    for repo, row in formats.COMPONENT_REPOS.items():
        assert isinstance(row.get("file"), str) and row["file"], repo
    assert vad.FILE == formats.COMPONENT_REPOS[vad.REPO]["file"]
    assert diarize.SEGMENTATION_FILE == formats.COMPONENT_REPOS[
        diarize.SEGMENTATION_REPO]["file"]
    assert diarize.EMBEDDING_FILE == formats.COMPONENT_REPOS[
        diarize.EMBEDDING_REPO]["file"]
    for module in (vad, diarize):
        with open(module.__file__, encoding="utf-8") as handle:
            assert "download_snapshot(" not in handle.read(), module.__name__


def test_llama_text_fetches_one_gguf_file_and_never_a_snapshot(monkeypatch):
    from fused_render.ai.runners import llama_text

    with open(llama_text.__file__, encoding="utf-8") as handle:
        source = handle.read()
    assert "worker_base.download_snapshot(" not in source
    assert "worker_base.download_file(" in source


def test_onnx_embed_fetches_an_allow_list_of_graphs_and_metadata():
    """Already scoped before this change: `download` passes `allow_patterns` built
    from the listing (one graph per tower, its sidecar, tokenizer metadata).
    Behaviour is pinned in `test_ai_onnx_embed_worker.py`; this is the table row."""
    from fused_render.ai.runners import onnx_embed

    with open(onnx_embed.__file__, encoding="utf-8") as handle:
        source = handle.read()
    assert "allow_patterns=list(_METADATA_PATTERNS) + list(weights)" in source


def test_a_curated_diffusers_recipe_stays_an_allow_list():
    from fused_render.ai.runners import torch_image

    for model_id, recipe in torch_image._GGUF_RECIPES.items():
        assert "model_index.json" in recipe["keep"], model_id
        assert not any("*" == pattern for pattern in recipe["keep"]), model_id
