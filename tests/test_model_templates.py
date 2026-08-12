"""The model_card template (SPEC §38) — the card and its tokenizer section.

It is bound to the universal `/` registry key, so its `condition.py` runs on
EVERY directory the user opens — which makes the gate the part worth testing
hardest. A gate that is too eager offers a model view on someone's source tree;
one that is too slow taxes every folder open, including folders on remote
mounts, where the difference between a probe the listing can answer and one it
cannot is a remote round trip.

The two scripts are tested against synthetic model folders rather than real
downloads: a safetensors file is nothing but its header for these purposes, so
a "12B model" here is a JSON header claiming 12B worth of shapes.
"""
import json
import os
import sys

import pytest

TEMPLATES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fused_render", "templates"
)


def _load(template, module):
    """Import a template's sibling .py the way the executor does — by path, not
    as part of the package (a template is a set of scripts, never an import of
    fused_render)."""
    import importlib.util

    path = os.path.join(TEMPLATES, template, module + ".py")
    spec = importlib.util.spec_from_file_location(f"_tmpl_{template}_{module}", path)
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


@pytest.fixture(scope="module")
def card_condition():
    return _load("model_card", "condition")


@pytest.fixture(scope="module")
def inspector():
    return _load("model_card", "inspect_model")


@pytest.fixture(scope="module")
def tokenizer_reader():
    return _load("model_card", "tokenize_text")


def _safetensors(tensors):
    """A safetensors file that is only its header — shapes are all the reader
    needs, so the weights themselves never have to exist."""
    header = {
        name: {"dtype": dtype, "shape": list(shape), "data_offsets": [0, 0]}
        for name, (dtype, shape) in tensors.items()
    }
    header["__metadata__"] = {"format": "pt"}
    blob = json.dumps(header).encode()
    return len(blob).to_bytes(8, "little") + blob


def _write(root, rel, content):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content) if isinstance(content, bytes) else path.write_text(content)
    return path


# -- the gates -------------------------------------------------------------------


def test_a_cache_repo_folder_is_a_model(card_condition, tmp_path):
    repo = tmp_path / "models--openai--whisper-small"
    (repo / "snapshots" / "abc").mkdir(parents=True)
    assert card_condition.main(str(repo)) is True


def test_a_snapshot_with_a_model_config_is_a_model(card_condition, tmp_path):
    folder = tmp_path / "snap"
    folder.mkdir()
    _write(folder, "config.json", json.dumps({"architectures": ["LlamaForCausalLM"]}))
    assert card_condition.main(str(folder)) is True


def test_a_diffusers_pipeline_is_a_model(card_condition, tmp_path):
    folder = tmp_path / "pipe"
    folder.mkdir()
    _write(folder, "model_index.json", json.dumps({"_class_name": "FluxPipeline"}))
    assert card_condition.main(str(folder)) is True


def test_someone_elses_config_json_is_not_a_model(card_condition, tmp_path):
    # The reason the gate reads the file at all: config.json is one of the most
    # common filenames there is, and a folder of application settings must not
    # sprout a model view.
    folder = tmp_path / "my-app"
    folder.mkdir()
    _write(folder, "config.json", json.dumps({"port": 8080, "debug": True}))
    assert card_condition.main(str(folder)) is False


@pytest.mark.parametrize("content", ["not json at all", "", "[1, 2, 3]"])
def test_an_unreadable_config_fails_closed(card_condition, tmp_path, content):
    folder = tmp_path / "weird"
    folder.mkdir()
    _write(folder, "config.json", content)
    assert card_condition.main(str(folder)) is False


def test_a_plain_folder_and_a_file_are_not_models(card_condition, tmp_path):
    folder = tmp_path / "plain"
    folder.mkdir()
    _write(folder, "notes.txt", "hello")
    assert card_condition.main(str(folder)) is False
    assert card_condition.main(str(folder / "notes.txt")) is False


def test_the_card_gate_never_lists_the_directory(card_condition, tmp_path, monkeypatch):
    # It runs on every folder open, including folders on remote mounts where a
    # listing scales with entry count and blows past the mount's timeout. Same
    # rule zarr_aoi's gate documents.
    folder = tmp_path / "big"
    folder.mkdir()
    _write(folder, "config.json", json.dumps({"model_type": "llama"}))
    for banned in ("listdir", "scandir", "walk"):
        monkeypatch.setattr(
            card_condition.os, banned,
            lambda *a, **k: pytest.fail(f"the gate called os.{banned}"),
        )
    assert card_condition.main(str(folder)) is True


def test_a_folder_holding_only_a_tokenizer_is_still_a_model(card_condition, tmp_path):
    # The card tokenizes now, so a tokenizer-only repo — a real shape on the Hub
    # — is a folder this view has something to say about.
    modern = tmp_path / "modern"
    modern.mkdir()
    _write(modern, "tokenizer.json", "{}")
    assert card_condition.main(str(modern)) is True
    # The older vocab.txt/merges.txt pair needs the model class that owns it, so
    # nothing here could tokenize it — not offering beats offering broken.
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    _write(legacy, "vocab.txt", "hello\nworld\n")
    assert card_condition.main(str(legacy)) is False


def _cache_repo(tmp_path, name="models--org--m", commit="c0ffee", tokenizer="{}", ref="main"):
    """A Hugging Face cache repo: the shape the AI Models cards actually open —
    the tokenizer lives under `snapshots/<commit>/`, not in the folder itself."""
    repo = tmp_path / name
    if tokenizer is not None:
        _write(repo, os.path.join("snapshots", commit, "tokenizer.json"), tokenizer)
    else:
        (repo / "snapshots" / commit).mkdir(parents=True)
    if ref:
        _write(repo, os.path.join("refs", ref), commit)
    return repo


def test_the_gate_does_not_count_as_using_the_model(card_condition, tmp_path):
    # MV-5, on the gate as well as the reader: it runs on every folder the user
    # opens, so it is the LAST thing that should mark a model as recently used
    # and protect it from the next prune.
    folder = tmp_path / "m"
    _write(folder, "config.json", json.dumps({"model_type": "llama"}))
    config = folder / "config.json"
    old = 1_000_000
    os.utime(config, (old, old))
    assert card_condition.main(str(folder)) is True
    assert os.stat(config).st_atime == old


# -- the inspector ---------------------------------------------------------------


def test_a_cache_repo_is_described_through_its_main_revision(inspector, tmp_path):
    repo = tmp_path / "models--org--m"
    _write(repo, "snapshots/old/config.json", json.dumps({"architectures": ["BertForMaskedLM"]}))
    _write(repo, "snapshots/new/config.json", json.dumps({"architectures": ["LlamaForCausalLM"]}))
    _write(repo, "refs/main", "new")
    out = inspector.main(str(repo))
    assert out["name"] == "org/m" and out["kind"] == "model"
    assert out["revision"] == "new"
    assert out["config"]["architectures"] == ["LlamaForCausalLM"]
    # Listed in name order ("new" then "old"), with the one refs/main points at
    # flagged — the view describes that one.
    assert [r["commit"] for r in out["revisions"]] == ["new", "old"]
    assert [r["current"] for r in out["revisions"]] == [True, False]


def test_parameters_come_from_the_headers_across_components(inspector, tmp_path):
    folder = tmp_path / "pipe"
    _write(folder, "model_index.json", json.dumps({"_class_name": "FluxPipeline"}))
    _write(folder, "transformer/diffusion_pytorch_model.safetensors",
           _safetensors({"blocks": ("BF16", (1000, 1000))}))
    _write(folder, "vae/diffusion_pytorch_model.safetensors",
           _safetensors({"conv": ("BF16", (100, 100))}))
    out = inspector.main(str(folder))
    assert out["params"]["total"] == 1000 * 1000 + 100 * 100
    assert out["params"]["estimated"] is False
    assert {w["file"] for w in out["weights"]} == {
        "transformer/diffusion_pytorch_model.safetensors", "vae/diffusion_pytorch_model.safetensors"}
    # Largest first, so the biggest tensor leads the table.
    assert out["largest"][0]["name"] == "blocks"


def test_a_quantized_checkpoint_reports_weights_not_storage_slots(inspector, tmp_path):
    folder = tmp_path / "q"
    _write(folder, "config.json", json.dumps({
        "architectures": ["Gemma3ForCausalLM"], "quantization": {"group_size": 64, "bits": 4}}))
    _write(folder, "model.safetensors", _safetensors({"w": ("U32", (1000, 500))}))
    out = inspector.main(str(folder))
    assert out["params"]["total"] == 1000 * 500 * 8  # eight 4-bit weights per word
    assert out["params"]["estimated"] is True
    assert out["config"]["quantization"] == "4-bit"


def test_the_model_card_front_matter_and_summary_are_read(inspector, tmp_path):
    folder = tmp_path / "m"
    _write(folder, "config.json", json.dumps({"model_type": "llama"}))
    _write(folder, "README.md",
           "---\nlicense: apache-2.0\npipeline_tag: text-generation\ntags:\n  - chat\n  - tiny\n---\n"
           "# Heading\n\n![badge](x.png)\n\nA small model for testing.\n\nMore prose.\n")
    card = inspector.main(str(folder))["card"]
    assert card["license"] == "apache-2.0"
    assert card["pipelineTag"] == "text-generation"
    assert card["tags"] == ["chat", "tiny"]
    # The first real paragraph — headings and badges are noise on a card.
    assert card["summary"] == "A small model for testing."


def test_inspecting_a_model_does_not_count_as_using_it(inspector, tmp_path):
    # Same rule as the AI Models page (SPEC HF-15): "last read" is what pruning
    # by age is built on, so looking at a model must not protect it from the
    # next prune.
    folder = tmp_path / "m"
    _write(folder, "config.json", json.dumps({"model_type": "llama"}))
    _write(folder, "README.md", "---\nlicense: mit\n---\n")
    _write(folder, "model.safetensors", _safetensors({"w": ("F16", (8, 8))}))
    old = 1_000_000
    for name in ("config.json", "README.md", "model.safetensors"):
        os.utime(folder / name, (old, old))
    inspector.main(str(folder))
    for name in ("config.json", "README.md", "model.safetensors"):
        assert os.stat(folder / name).st_atime == old, name


def test_the_same_blob_under_two_names_is_counted_once(inspector, tmp_path):
    folder = tmp_path / "m"
    _write(folder, "config.json", json.dumps({"model_type": "llama"}))
    _write(folder, "model.safetensors", _safetensors({"w": ("F16", (64, 64))}))
    try:
        os.link(folder / "model.safetensors", folder / "consolidated.safetensors")
    except (OSError, NotImplementedError):
        pytest.skip("filesystem does not support hard links")
    assert inspector.main(str(folder))["params"]["total"] == 64 * 64


# -- the tokenizer reader --------------------------------------------------------


def _tokenizer_json(vocab, merges=None, specials=()):
    return json.dumps({
        "model": {"type": "BPE", "vocab": vocab, "merges": merges or []},
        "added_tokens": [{"content": s, "special": True} for s in specials],
    })


def test_tokenizer_facts_need_no_library(tokenizer_reader, tmp_path):
    # The half that always works: vocabulary, kind and special tokens are read
    # from the JSON, so a machine without `tokenizers` still learns something.
    folder = tmp_path / "m"
    folder.mkdir()
    _write(folder, "tokenizer.json",
           _tokenizer_json({"a": 0, "b": 1, "c": 2}, merges=["a b"], specials=["<eos>"]))
    out = tokenizer_reader.main(str(folder), "", facts=True)
    assert out["facts"]["vocabSize"] == 3
    assert out["facts"]["kind"] == "BPE"
    assert out["facts"]["merges"] == 1
    assert out["facts"]["specialTokens"] == ["<eos>"]


def test_a_missing_library_is_reported_not_raised(tokenizer_reader, tmp_path, monkeypatch):
    # `tokenizers` arrives through the folder's pyproject and only under the
    # fused engine, so its absence is a state to explain, not a crash.
    folder = tmp_path / "m"
    folder.mkdir()
    _write(folder, "tokenizer.json", _tokenizer_json({"a": 0}))
    monkeypatch.setattr(tokenizer_reader, "_load", lambda path: None)
    out = tokenizer_reader.main(str(folder), "hello", facts=True)
    assert out["available"] is False
    assert out["tokens"] == []
    assert out["facts"]["vocabSize"] == 1  # …and the facts still came through


def test_a_folder_without_a_tokenizer_says_so(tokenizer_reader, tmp_path):
    folder = tmp_path / "m"
    folder.mkdir()
    assert "error" in tokenizer_reader.main(str(folder), "hi")


def test_the_card_resolves_the_revision_and_the_encoder_is_handed_it(inspector, tokenizer_reader,
                                                                    tmp_path):
    # ONE owner for the cache layout. The page opens the REPO folder; the card
    # works out which revision refs/main names and reports `root`, and the
    # encoder is given that answer rather than resolving it again from a second
    # copy of the rule that could drift from the first.
    repo = _cache_repo(tmp_path, tokenizer=_tokenizer_json({"a": 0, "b": 1}))
    card = inspector.main(str(repo))
    assert card["hasTokenizer"] is True
    assert card["root"].endswith("snapshots/c0ffee")
    out = tokenizer_reader.main(card["root"], "", facts=True)
    assert "error" not in out
    assert out["facts"]["vocabSize"] == 2


def test_a_model_without_a_tokenizer_gets_no_section(inspector, tmp_path):
    repo = _cache_repo(tmp_path, tokenizer=None)
    assert inspector.main(str(repo))["hasTokenizer"] is False


def test_the_facts_are_read_once_not_on_every_keystroke(tokenizer_reader, tmp_path):
    # tokenizer.json is routinely tens of MB, and nothing survives between calls
    # (each is a fresh subprocess, PY-6). What keeps typing responsive is that
    # the page asks for the description on its FIRST call and never again.
    folder = tmp_path / "m"
    folder.mkdir()
    _write(folder, "tokenizer.json", _tokenizer_json({"a": 0}))
    assert "facts" in tokenizer_reader.main(str(folder), "hi", facts=True)
    keystroke = tokenizer_reader.main(str(folder), "hi")
    assert "facts" not in keystroke  # the whole-file parse is skipped entirely


def test_a_facts_only_call_never_loads_the_tokenizer(tokenizer_reader, tmp_path, monkeypatch):
    # With no text there is nothing to encode, so there is nothing to load. The
    # library's own loader reads the same tens of MB the facts parse just read;
    # paying for it and throwing the result away is the cost this split exists
    # to avoid.
    folder = tmp_path / "m"
    folder.mkdir()
    _write(folder, "tokenizer.json", _tokenizer_json({"a": 0}))
    monkeypatch.setattr(
        tokenizer_reader, "_load",
        lambda path: pytest.fail("a facts-only call loaded the tokenizer"))
    out = tokenizer_reader.main(str(folder), "", facts=True)
    assert out["facts"]["vocabSize"] == 1 and out["tokens"] == []


def test_tokenizing_does_not_count_as_using_the_model(tokenizer_reader, tmp_path):
    # MV-5 again: the facts parse and the library's own load both read the file,
    # so both put the atime back.
    repo = _cache_repo(tmp_path, tokenizer=_tokenizer_json({"a": 0}))
    root = repo / "snapshots" / "c0ffee"
    tokenizer = root / "tokenizer.json"
    old = 1_000_000
    os.utime(tokenizer, (old, old))
    tokenizer_reader.main(str(root), "hello", facts=True)
    assert os.stat(tokenizer).st_atime == old


def test_encoding_reports_pieces_ids_and_offsets(tokenizer_reader, tmp_path):
    pytest.importorskip("tokenizers")
    from tokenizers import Tokenizer, models

    folder = tmp_path / "m"
    folder.mkdir()
    tokenizer = Tokenizer(models.WordLevel({"hello": 0, "world": 1}, unk_token="[UNK]"))
    from tokenizers import pre_tokenizers

    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer.save(str(folder / "tokenizer.json"))
    out = tokenizer_reader.main(str(folder), "hello world")
    assert out["available"] is True
    assert [t["piece"] for t in out["tokens"]] == ["hello", "world"]
    assert out["count"] == 2
    # Offsets index the ORIGINAL text, which is what lets the page highlight
    # what was typed rather than the rewritten piece.
    assert [out["text"][t["start"]:t["end"]] for t in out["tokens"]] == ["hello", "world"]


def test_a_snapshot_opened_directly_still_knows_the_model_it_belongs_to(inspector, tmp_path):
    # Browsing into `…/<repo>/snapshots/<commit>` from the listing is the normal
    # way to arrive here, and a commit sha is not a model's name.
    repo = tmp_path / "models--meta-llama--Llama-3.1-8B"
    snapshot = repo / "snapshots" / "bb22"
    _write(snapshot, "config.json", json.dumps({"model_type": "llama"}))
    out = inspector.main(str(snapshot))
    assert out["name"] == "meta-llama/Llama-3.1-8B"
    assert out["kind"] == "model"
    assert out["revision"] == "bb22"


def test_a_folder_that_is_not_in_a_cache_keeps_its_own_name(inspector, tmp_path):
    folder = tmp_path / "my-finetune"
    _write(folder, "config.json", json.dumps({"model_type": "llama"}))
    out = inspector.main(str(folder))
    assert out["name"] == "my-finetune" and out["kind"] == "folder"
    # …and gets NO Hub link: it is somebody's own checkout, and a link built
    # from a local directory name would point at a stranger's repo.
    assert out["hubUrl"] is None


@pytest.mark.parametrize("dirname,kind,url", [
    ("models--meta-llama--Llama-3.1-8B", "model",
     "https://huggingface.co/meta-llama/Llama-3.1-8B"),
    ("datasets--org--squad", "dataset", "https://huggingface.co/datasets/org/squad"),
    ("spaces--org--demo", "space", "https://huggingface.co/spaces/org/demo"),
])
def test_the_card_links_back_to_the_hub(inspector, tmp_path, dirname, kind, url):
    # The way OUT of the view: it reads this disk, while the licence, the
    # discussions and every revision live on the Hub page it cannot show. The
    # KIND decides the path — a dataset linked as huggingface.co/<id> is a 404
    # dressed up as a link.
    repo = tmp_path / dirname
    _write(repo, "snapshots/c1/config.json", json.dumps({"model_type": "llama"}))
    _write(repo, "refs/main", "c1")
    out = inspector.main(str(repo))
    assert out["kind"] == kind
    assert out["hubUrl"] == url


def test_a_tokenizer_the_library_refuses_is_explained(tokenizer_reader, tmp_path):
    # A vocabulary/merge mismatch, a version this build cannot read, a file that
    # is JSON but not a tokenizer: the page must say so and keep the facts, not
    # hand the user a Rust traceback.
    pytest.importorskip("tokenizers")
    folder = tmp_path / "m"
    folder.mkdir()
    _write(folder, "tokenizer.json", json.dumps({
        "model": {"type": "BPE", "vocab": {"x": 0}, "merges": ["a b"]},  # merge names no real token
        "added_tokens": [],
    }))
    out = tokenizer_reader.main(str(folder), "hello", facts=True)
    assert out["available"] is False
    assert out["loadError"]
    assert out["facts"]["vocabSize"] == 1
