"""App-declared AI models (`[tool.fused-render.app.models]`): the parser, the
per-app accepted/substitution state, and the runtime resolver. The overview
endpoints and the AI-route substitution are covered further down."""
import json
import os

import pytest

from fused_render import app_models, background_apps


def _app(tmp_path, toml_body, name="app"):
    d = tmp_path / name
    d.mkdir()
    (d / "pyproject.toml").write_text(toml_body)
    (d / "index.html").write_text("<html></html>")
    return str(d)


ALL_VERBS = """
[tool.fused-render.app.models]
text = { model = "Qwen/Qwen3-4B-Instruct-GGUF", use = "Captions" }
image = { model = "black-forest-labs/FLUX.2-klein-4B", swappable = true }
video = { model = "v/video" }
speech = { model = "s/speech" }
transcribe = { model = "t/whisper" }
embed = { model = "e/embed" }
decide = { model = "d/decide" }
vision = { model = "v/vision" }
"""


def test_declaration_normalizes_all_verbs(tmp_path):
    d = _app(tmp_path, ALL_VERBS)
    decl = app_models.load_declaration(d)
    assert set(decl) == {
        "text-generation", "text-to-image", "text-to-video", "text-to-speech",
        "automatic-speech-recognition", "embeddings", "text-classification",
        "image-to-text",
    }
    img = decl["text-to-image"]
    assert img == {"verb": "image", "model": "black-forest-labs/FLUX.2-klein-4B",
                   "swappable": True, "use": None}
    txt = decl["text-generation"]
    assert txt["swappable"] is False and txt["use"] == "Captions"


def test_unknown_key_and_bad_entries_ignored(tmp_path):
    d = _app(tmp_path, """
[tool.fused-render.app.models]
bogus = { model = "x/y" }
image = "not-a-table"
text = { model = "" }
speech = { model = "s/speech" }
""")
    assert list(app_models.load_declaration(d)) == ["text-to-speech"]


def test_invalid_toml_and_missing_file(tmp_path):
    d = _app(tmp_path, "this is [not toml")
    assert app_models.load_declaration(d) == {}
    assert app_models.load_declaration(str(tmp_path / "nope")) == {}


def test_models_only_pyproject_is_not_a_background_app(tmp_path):
    d = _app(tmp_path, ALL_VERBS)
    assert background_apps.load_manifest(d) is None


def test_hash_rules(tmp_path):
    a = app_models.declaration_hash(app_models.load_declaration(_app(tmp_path, ALL_VERBS, "a")))
    # `use` is a label only
    relabeled = ALL_VERBS.replace('use = "Captions"', 'use = "Other"')
    b = app_models.declaration_hash(app_models.load_declaration(_app(tmp_path, relabeled, "b")))
    assert a == b
    swapped = ALL_VERBS.replace("swappable = true", "swappable = false")
    c = app_models.declaration_hash(app_models.load_declaration(_app(tmp_path, swapped, "c")))
    assert c != a
    changed = ALL_VERBS.replace("v/video", "v/video2")
    d_ = app_models.declaration_hash(app_models.load_declaration(_app(tmp_path, changed, "d")))
    assert d_ != a
    more = ALL_VERBS + '\n'
    extra = _app(tmp_path, more.replace('video = { model = "v/video" }\n', ""), "e")
    assert app_models.declaration_hash(app_models.load_declaration(extra)) != a


def test_state_roundtrip(tmp_path):
    d = _app(tmp_path, ALL_VERBS)
    assert app_models.read_state(d) == {}
    app_models.write_state(d, True, "h", {"text-to-image": "x/y"})
    assert app_models.read_state(d) == {
        "accepted": True, "hash": "h", "substitutions": {"text-to-image": "x/y"}}
    with open(os.path.join(d, ".fused", "models.json"), "w") as f:
        f.write("{corrupt")
    assert app_models.read_state(d) == {}


def test_find_app_dir_from_nested_page(tmp_path):
    d = _app(tmp_path, ALL_VERBS)
    sub = os.path.join(d, "pages")
    os.makedirs(sub)
    page = os.path.join(sub, "x.html")
    open(page, "w").write("<html/>")
    assert app_models.find_app_dir(page) == d
    assert app_models.find_app_dir(d) == d
    other = tmp_path / "plain"
    other.mkdir()
    assert app_models.find_app_dir(str(other / "a.html")) is None


def _accepted(tmp_path, subs, toml=ALL_VERBS):
    d = _app(tmp_path, toml)
    decl = app_models.load_declaration(d)
    app_models.write_state(d, True, app_models.declaration_hash(decl), subs)
    return d


IMG = "black-forest-labs/FLUX.2-klein-4B"


def test_resolve_rules(tmp_path):
    d = _accepted(tmp_path, {"text-to-image": "other/img"})
    page = os.path.join(d, "index.html")
    cap = "text-to-image"
    assert app_models.resolve(page, cap, IMG) == "other/img"
    assert app_models.resolve(page, cap, None) == "other/img"
    assert app_models.resolve(page, cap, "") == "other/img"
    assert app_models.resolve(page, cap, "undeclared/model") == "undeclared/model"
    assert app_models.resolve(page, "text-to-video", "v/video") == "v/video"
    assert app_models.resolve(page, cap, None) == "other/img"
    assert app_models.substitution_for(page, "text-to-speech", None) is None


def test_resolve_ignores_unaccepted_and_stale(tmp_path):
    d = _app(tmp_path, ALL_VERBS)
    page = os.path.join(d, "index.html")
    app_models.write_state(d, False, "x", {"text-to-image": "other/img"})
    assert app_models.resolve(page, "text-to-image", IMG) == IMG
    decl = app_models.load_declaration(d)
    app_models.write_state(d, True, "stale-hash", {"text-to-image": "other/img"})
    assert not app_models.is_accepted(d, decl)
    assert app_models.resolve(page, "text-to-image", IMG) == IMG
    assert app_models.resolve(None, "text-to-image", None) is None
    assert app_models.resolve("/nonexistent/p.html", "text-to-image", "a/b") == "a/b"
