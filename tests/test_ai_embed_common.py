"""`embed_common.py` — the request shape and the arithmetic both embedding
runners share (SPEC §40).

Plain functions over plain data, so this is driven directly rather than
through either runner's own `sys.path` dance (`tests/test_ai_mlx_worker.py`'s
style) — `embed_common` has no engine of its own to fake.
"""
import sys

import re

import pytest
from PIL import Image

from fused_render.ai.runners import embed_common, formats


# -- request_kind ---------------------------------------------------------------


def test_texts_alone_is_accepted():
    assert embed_common.request_kind({"texts": ["a", "b"]}) == (
        "texts", ["a", "b"], "document")


def test_paths_alone_is_accepted():
    assert embed_common.request_kind({"paths": ["/a.png"]}) == (
        "paths", ["/a.png"], "document")


def test_neither_is_refused():
    with pytest.raises(ValueError, match="texts.*paths"):
        embed_common.request_kind({})


def test_an_empty_body_is_the_same_refusal_as_neither():
    """The router's own "no body" case: `{}` and "neither key present" are the
    identical shape, so there is exactly one code path refusing both."""
    with pytest.raises(ValueError):
        embed_common.request_kind({})


def test_both_keys_is_refused():
    with pytest.raises(ValueError, match="not both"):
        embed_common.request_kind({"texts": ["a"], "paths": ["/a.png"]})


def test_an_empty_list_is_refused():
    with pytest.raises(ValueError):
        embed_common.request_kind({"texts": []})


def test_over_max_items_is_refused():
    with pytest.raises(ValueError, match="64"):
        embed_common.request_kind({"texts": ["x"] * (embed_common.MAX_ITEMS + 1)})


def test_exactly_max_items_is_accepted():
    source, items, _kind = embed_common.request_kind(
        {"texts": ["x"] * embed_common.MAX_ITEMS})
    assert source == "texts"
    assert len(items) == embed_common.MAX_ITEMS


def test_a_non_string_item_is_refused():
    with pytest.raises(ValueError, match=r"texts\[1\]"):
        embed_common.request_kind({"texts": ["fine", 42]})


def test_an_empty_string_item_is_refused():
    with pytest.raises(ValueError, match=r"paths\[0\]"):
        embed_common.request_kind({"paths": [""]})


# -- unit_normalize ---------------------------------------------------------------


def test_every_row_lands_at_unit_length():
    vectors = embed_common.unit_normalize([[3.0, 4.0], [1.0, 0.0], [0.0, -2.0]])
    for row in vectors:
        norm = sum(v * v for v in row) ** 0.5
        assert abs(norm - 1.0) < 1e-9


def test_direction_is_preserved():
    vectors = embed_common.unit_normalize([[3.0, 4.0]])
    assert vectors[0][0] == pytest.approx(0.6)
    assert vectors[0][1] == pytest.approx(0.8)


def test_a_zero_vector_is_left_as_zero_not_divided():
    """A real model never emits one; a mocked model in a test might."""
    vectors = embed_common.unit_normalize([[0.0, 0.0, 0.0]])
    assert vectors == [[0.0, 0.0, 0.0]]


def test_the_result_is_plain_python_floats():
    vectors = embed_common.unit_normalize([[3, 4]])
    assert all(isinstance(v, float) for v in vectors[0])


# -- open_image ---------------------------------------------------------------


def test_open_image_reads_a_real_picture_as_rgb(tmp_path):
    path = tmp_path / "pic.png"
    Image.new("RGBA", (4, 4), (10, 20, 30, 255)).save(path)
    image = embed_common.open_image(str(path))
    assert image.mode == "RGB"
    assert image.size == (4, 4)


def test_open_image_names_a_missing_file(tmp_path):
    missing = tmp_path / "nope.png"
    with pytest.raises(ValueError, match=re.escape(str(missing))):
        embed_common.open_image(str(missing))


def test_open_image_names_a_directory(tmp_path):
    with pytest.raises(ValueError, match=re.escape(str(tmp_path))):
        embed_common.open_image(str(tmp_path))


def test_open_image_refuses_a_non_image_file(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("this is not a picture")
    with pytest.raises(ValueError, match="not a readable image"):
        embed_common.open_image(str(path))


def test_a_truncated_image_is_decoded_rather_than_refused(tmp_path):
    """A half-written JPEG must not take a batch of 64 down with it.

    Photo libraries are full of them — interrupted downloads, a messaging app's
    cache — and PIL's default is `OSError: image file is truncated`. Reported
    from a real run over a WhatsApp cache directory, where one such file aborted
    the whole embed call and stopped indexing on a file the user cannot act on.
    """
    from PIL import Image

    whole = tmp_path / "whole.jpg"
    Image.new("RGB", (64, 64), (10, 120, 200)).save(whole, quality=95)
    cut = tmp_path / "cut.jpg"
    data = whole.read_bytes()
    cut.write_bytes(data[:-2])                      # lose the tail, as reported

    image = embed_common.open_image(str(cut))
    assert image.mode == "RGB"
    assert image.size == (64, 64)


def test_heic_is_registered_so_a_photo_library_is_readable(monkeypatch):
    """HEIC is the iPhone default, and plain Pillow cannot open it.

    Registration is what makes `paths` work on photographs rather than only on
    screenshots — measured on a real library, the first `.HEIC` came back "is
    not a readable image" before this. Asserted through the module's own flag
    and a stubbed opener, so the test does not need pillow-heif installed here.
    """
    calls = []
    fake = type(sys)("pillow_heif")
    fake.register_heif_opener = lambda: calls.append(1)
    monkeypatch.setitem(sys.modules, "pillow_heif", fake)
    monkeypatch.setattr(embed_common, "_heif_registered", False)

    embed_common._register_heif()
    embed_common._register_heif()          # once per process, not per image
    assert calls == [1]


def test_a_missing_pillow_heif_does_not_break_other_formats(monkeypatch, tmp_path):
    """An engine whose venv predates the dependency must still embed PNGs."""
    from PIL import Image

    monkeypatch.setitem(sys.modules, "pillow_heif", None)   # import raises
    monkeypatch.setattr(embed_common, "_heif_registered", False)
    path = tmp_path / "ok.png"
    Image.new("RGB", (8, 8), (4, 5, 6)).save(path)

    assert embed_common.open_image(str(path)).mode == "RGB"


# -- the retrieval `kind`, ported from PR #780 ----------------------------------
#
# `request_kind` returns THREE values now, and the third is the retrieval kind.
# See its own docstring for why this is a wider tuple rather than a second
# function: a caller that forgot to ask would silently ignore `kind`, which is
# the whole failure this parameter exists to prevent.


def test_the_default_kind_is_document():
    _source, _items, kind = embed_common.request_kind({"texts": ["a"]})
    assert kind == "document"
    assert kind == embed_common.DEFAULT_KIND
    assert kind == formats.TEXT_EMBED_DEFAULT_KIND


@pytest.mark.parametrize("asked", ["query", "document"])
def test_both_kinds_are_accepted(asked):
    _source, _items, kind = embed_common.request_kind(
        {"texts": ["a"], "kind": asked})
    assert kind == asked


def test_an_unrecognised_kind_is_REFUSED_rather_than_defaulted():
    """**The one field here whose wrong value produces no error anywhere
    downstream.** A `kind: "queries"` that fell through to the default would
    return unit-length vectors of the right dimension, computed against the
    wrong half of the prompt pair, and nothing the caller could measure would
    say so. Refusing costs a typo one round trip; defaulting costs a silent
    accuracy regression nobody attributes to this call.
    """
    with pytest.raises(ValueError) as excinfo:
        embed_common.request_kind({"texts": ["a"], "kind": "queries"})
    message = str(excinfo.value)
    assert "'query'" in message and "'document'" in message
    # It says what the default is, so the fix for "I did not mean to pass this"
    # is in the message rather than in the docs.
    assert "document" in message


def test_a_kind_on_an_IMAGE_request_is_refused_by_name():
    """A prompt scheme instructs TEXT — "search_query: " glued to the front of
    a sentence. There is nothing for it to prefix on an image, so a `kind`
    beside `paths` is a caller who has misunderstood the parameter, and
    accepting it would mean silently ignoring the only field they set.
    """
    with pytest.raises(ValueError) as excinfo:
        embed_common.request_kind({"paths": ["/a.png"], "kind": "query"})
    assert "kind" in str(excinfo.value)
    assert "texts" in str(excinfo.value)


def test_an_explicit_null_kind_means_the_default_rather_than_a_refusal():
    """`None` is JSON's "I did not say" and the bridge forwards `undefined` as
    an absent key; a page that builds its body with `kind: someState || null`
    must not get a 400 for it."""
    _source, _items, kind = embed_common.request_kind(
        {"texts": ["a"], "kind": None})
    assert kind == "document"


# -- the prompt table ----------------------------------------------------------


def test_every_scheme_has_two_halves_and_at_least_one_of_them_differs():
    """A scheme whose two prefixes were equal would be a `"none"` with extra
    steps — it would claim asymmetry the model does not have, and `kind` would
    be a parameter with no effect on it."""
    for name, pair in formats.TEXT_EMBED_PROMPTS.items():
        assert len(pair) == 2, name
        if name == "none":
            assert pair == ("", "")
        else:
            assert pair[0] != pair[1], name


def test_prompted_glues_the_right_half_on():
    assert embed_common.prompted(["hi"], "query", "e5") == ["query: hi"]
    assert embed_common.prompted(["hi"], "document", "e5") == ["passage: hi"]


def test_bges_document_side_is_genuinely_empty():
    """The tie-breaker behind the "document" default: bge's card instructs the
    QUERY only, so on that family a bare call embeds text verbatim — exactly
    what someone who has never heard of prompt schemes expects to happen."""
    assert embed_common.prompted(["hi"], "document", "bge") == ["hi"]
    assert embed_common.prompted(["hi"], "query", "bge") != ["hi"]


def test_an_unknown_scheme_embeds_verbatim_rather_than_guessing():
    """Reached from a worker holding a resident model with a validated batch in
    hand. A scheme name that has drifted out of the table is a reason to embed
    plainly, not to fail a call that would otherwise work — and guessing a
    prefix would be worse than not prefixing, since a wrong instruction is text
    the model dutifully encodes as content."""
    assert embed_common.prompted(["hi"], "query", "not-a-scheme") == ["hi"]


def test_prompted_returns_a_new_list_even_when_the_prefix_is_empty():
    """The caller may hold the original for its own reporting, so this must not
    hand back the same object under the `"none"` scheme."""
    texts = ["hi"]
    assert embed_common.prompted(texts, "query", "none") is not texts


# -- the scheme lookup ---------------------------------------------------------


def test_a_curated_id_takes_its_scheme_from_the_table_not_the_heuristic():
    for repo_id, scheme in formats.TEXT_EMBED_SCHEMES.items():
        assert formats.text_embed_scheme(repo_id) == scheme, repo_id
        assert scheme in formats.TEXT_EMBED_PROMPTS, repo_id


@pytest.mark.parametrize("repo_id,expected", [
    ("BAAI/bge-small-en-v1.5", "bge"),
    ("BAAI/BGE-large-en-v1.5", "bge"),            # uploaders capitalise freely
    ("intfloat/multilingual-e5-large", "e5"),
    ("nomic-ai/nomic-embed-text-v1", "nomic"),
    ("Qwen/Qwen3-Embedding-4B", "qwen3"),
    ("google/embeddinggemma-300m", "gemma-embedding"),
    # bge-m3 is the family member that takes NO query instruction — its card
    # says so outright — so it must not inherit `bge` by substring.
    ("BAAI/bge-m3", "none"),
    ("someone/something-nobody-here-knows", "none"),
    # A dual encoder has no retrieval scheme at all, and `"none"` is how the
    # route knows to refuse `kind` for it.
    ("google/siglip2-base-patch16-384", "none"),
    ("onnx-community/siglip2-base-patch16-384-ONNX", "none"),
])
def test_the_repo_id_heuristic(repo_id, expected):
    """A documented heuristic, and the reason it is allowed to be one: a model's
    prompt convention is a fact about its training that no file in the snapshot
    records, so for an uncurated repo the id is the only evidence there is. It
    is reported back on the catalog entry rather than applied out of sight,
    which is what makes it auditable instead of silent."""
    assert formats.text_embed_scheme(repo_id) == expected
