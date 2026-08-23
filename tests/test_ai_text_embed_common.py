"""The one request shape all three text-embedding runners accept, and the
prompt rules that decide what their vectors mean (SPEC §40).

`runners/text_embed_common.py` is imported by three separate interpreters —
`llamacpp_embed/`, `llamacpp_embed_vulkan/` and `mlx_text_embed/` — and by the
server process, so everything asserted here is a rule that must read the same
on all four. Its sibling `tests/test_ai_embed_common.py` does the same job for
the DUAL-ENCODER capability's own validator; the two files are separate for
the same reason the two modules are (see this one's docstring).

Imported the packaged way rather than by path, unlike the runner tests: this
module is genuinely reachable as `fused_render.ai.runners.text_embed_common`
from the server, and that reading is itself worth exercising — its two-loader
import guard is the thing that makes it work at all.
"""
import math

import pytest

from fused_render.ai.runners import formats, text_embed_common as tec


# -- the shape ----------------------------------------------------------------


def test_a_plain_batch_is_accepted_with_the_default_kind():
    texts, kind = tec.request_texts({"texts": ["a", "b"]})
    assert texts == ["a", "b"]
    assert kind == "document"


def test_an_empty_body_is_refused():
    with pytest.raises(ValueError, match="non-empty list"):
        tec.request_texts({})


def test_an_empty_list_is_refused():
    with pytest.raises(ValueError, match="non-empty list"):
        tec.request_texts({"texts": []})


def test_a_non_string_item_names_its_index():
    """The index, not just "bad input" — a caller with a 64-item batch built
    from a directory listing needs to know WHICH one."""
    with pytest.raises(ValueError, match=r"texts\[1\]"):
        tec.request_texts({"texts": ["fine", 7]})


def test_the_batch_ceiling_is_shared_with_the_other_capability():
    """`MAX_ITEMS` is taken from `embed_common` rather than restated. The
    number is a fact about how much work one HTTP request should do before a
    caller would rather see progress — not a fact about dual encoders — so
    the two capabilities answering differently would be an inconsistency with
    no reason behind it."""
    from fused_render.ai.runners import embed_common

    assert tec.MAX_ITEMS == embed_common.MAX_ITEMS
    with pytest.raises(ValueError, match="at most 64"):
        tec.request_texts({"texts": ["x"] * (tec.MAX_ITEMS + 1)})
    # …and exactly the ceiling is fine, which is the off-by-one worth pinning.
    texts, _kind = tec.request_texts({"texts": ["x"] * tec.MAX_ITEMS})
    assert len(texts) == tec.MAX_ITEMS


# -- images, refused by name --------------------------------------------------


@pytest.mark.parametrize("field", ["paths", "images"])
def test_image_input_is_refused_with_a_sentence_that_points_somewhere(field):
    """**Not "unexpected field".** A caller passing `paths` here is not making
    a typo — they found `fused.ai.embed`'s image half and reasonably assumed
    this endpoint had one. So the refusal has to say which endpoint DOES,
    and say what would otherwise happen: a text encoder handed filenames
    embeds them as PROSE and returns vectors that look fine and mean nothing.
    """
    with pytest.raises(ValueError) as excinfo:
        tec.request_texts({"texts": ["a"], field: ["/tmp/cat.png"]})
    message = str(excinfo.value)
    assert field in message
    assert "fused.ai.embed()" in message
    assert "vision tower" in message


# -- kind ---------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["query", "document"])
def test_both_kinds_are_accepted(kind):
    _texts, got = tec.request_texts({"texts": ["a"], "kind": kind})
    assert got == kind


def test_an_unrecognised_kind_is_REFUSED_rather_than_defaulted():
    """**The one field here whose wrong value produces no error anywhere
    downstream.** A `kind: "queries"` that fell through to the default would
    return unit-length vectors of the right dimension, computed against the
    wrong half of the prompt pair, and nothing the caller could measure would
    say so. Refusing costs a typo one round trip; defaulting costs a silent
    accuracy regression nobody attributes to this call.
    """
    with pytest.raises(ValueError) as excinfo:
        tec.request_texts({"texts": ["a"], "kind": "queries"})
    message = str(excinfo.value)
    assert "'query'" in message and "'document'" in message
    # It says what the default is, so the fix for "I did not mean to pass
    # this" is in the message rather than in the docs.
    assert "document" in message


def test_the_default_is_document_and_the_runners_agree_with_the_table():
    """One value, in one place. `formats` holds it because the runners read
    it from there and the route reports it; a second literal in this module
    would be the copy that goes stale."""
    assert tec.DEFAULT_KIND == "document"
    assert tec.DEFAULT_KIND == formats.TEXT_EMBED_DEFAULT_KIND
    assert set(tec.KINDS) == {"query", "document"}


# -- the prompts --------------------------------------------------------------


def test_every_curated_recipe_names_a_scheme_this_table_knows():
    """A curated model whose scheme has no entry would embed both sides
    verbatim while claiming a convention it does not apply."""
    for key, recipe in formats.TEXT_EMBED_RECIPES.items():
        assert recipe["scheme"] in formats.TEXT_EMBED_PROMPTS, key


def test_every_scheme_has_two_halves_and_at_least_one_of_them_differs():
    """A scheme whose two prefixes were equal would be a `"none"` with extra
    steps — it would claim asymmetry the model does not have, and `kind`
    would be a parameter with no effect on it."""
    for name, pair in formats.TEXT_EMBED_PROMPTS.items():
        assert len(pair) == 2, name
        if name == "none":
            assert pair == ("", "")
        else:
            assert pair[0] != pair[1], name


def test_prompted_glues_the_right_half_on():
    assert tec.prompted(["hi"], "query", "e5") == ["query: hi"]
    assert tec.prompted(["hi"], "document", "e5") == ["passage: hi"]


def test_bges_document_side_is_genuinely_empty():
    """The tie-breaker behind the "document" default: bge's card instructs the
    QUERY only, so on that family a bare call embeds text verbatim — exactly
    what someone who has never heard of prompt schemes expects to happen."""
    assert tec.prompted(["hi"], "document", "bge") == ["hi"]
    assert tec.prompted(["hi"], "query", "bge") != ["hi"]


def test_an_unknown_scheme_embeds_verbatim_rather_than_guessing():
    """Reached from a worker holding a resident model with a validated batch
    in hand. A scheme name that has drifted out of the table is a reason to
    embed plainly, not to fail a call that would otherwise work — and
    guessing a prefix would be worse than not prefixing, since a wrong
    instruction is text the model dutifully encodes as content."""
    assert tec.prompted(["hi"], "query", "not-a-scheme") == ["hi"]


# -- the scheme lookup --------------------------------------------------------


def test_a_curated_id_takes_its_scheme_from_the_table_not_the_filename():
    for key, recipe in formats.TEXT_EMBED_RECIPES.items():
        assert formats.text_embed_scheme(key) == recipe["scheme"], key


@pytest.mark.parametrize("filename,expected", [
    ("bge-small-en-v1.5-q8_0.gguf", "bge"),
    ("BGE-large-en-v1.5-Q8_0.gguf", "bge"),          # uploaders capitalise freely
    ("multilingual-e5-large-Q8_0.gguf", "e5"),
    ("nomic-embed-text-v1.5.Q4_K_M.gguf", "nomic"),
    ("Qwen3-Embedding-4B-Q8_0.gguf", "qwen3"),
    ("embeddinggemma-300M-Q8_0.gguf", "gemma-embedding"),
    # bge-m3 is the family member that takes NO query instruction — its card
    # says so outright — so it must not inherit `bge` by substring.
    ("bge-m3-Q8_0.gguf", "none"),
    ("something-nobody-here-knows.gguf", "none"),
])
def test_the_filename_heuristic(filename, expected):
    """A documented heuristic, and the reason it is allowed to be one: a
    model's prompt convention is a fact about its training that the GGUF
    header records nowhere, so for an uncurated repo the filename is the only
    evidence there is. It is reported back on every reply rather than applied
    out of sight, which is what makes it auditable instead of silent."""
    assert formats.text_embed_scheme("some-org/whatever", filename) == expected


# -- normalization ------------------------------------------------------------


def test_unit_normalize_makes_cosine_a_dot_product():
    """The one guarantee the bridge documents and pages are told to rely on."""
    rows = tec.unit_normalize([[3.0, 4.0], [1.0, 1.0, 1.0, 1.0]])
    for row in rows:
        assert math.isclose(math.sqrt(sum(v * v for v in row)), 1.0, rel_tol=1e-9)


def test_normalization_is_the_same_function_the_other_capability_uses():
    """Re-exported rather than reimplemented: two vectors from two
    capabilities must mean the same thing, and a second copy of this
    arithmetic is how they would come not to."""
    from fused_render.ai.runners import embed_common

    assert tec.unit_normalize([[3.0, 4.0]]) == embed_common.unit_normalize([[3.0, 4.0]])
