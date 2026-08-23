"""Does this capability actually RETRIEVE — the one thing a text encoder is
for, proved rather than assumed (SPEC §40).

Every other test in this family checks a contract: the request shape, the
refusals, the wire format, that a prefix reaches the model. None of them would
notice if the vectors coming back were noise of the right width. This file is
the one that would.

**The corpus is built so that a keyword match CANNOT pass it**, which is the
whole design and is itself asserted below (`test_the_corpus_defeats_keyword_
matching`). Two of the five queries share not one word with the passage that
answers them — "how do plants make food from sunlight" against a sentence
about photosynthesis and glucose, "keeping the bread culture alive" against
one about a sourdough starter — and word-overlap ranking scores 2/5 on the
set, one of those two by coincidence. A semantic encoder scores 5/5.

**Split into two tests on purpose, and only one of them needs a model.** The
corpus property is a fact about the strings and is checked everywhere, on
every CI run, for free. The retrieval itself needs real weights and a working
llama.cpp, so it skips where it cannot run rather than being deleted — see
`_llm` for exactly what it demands and why each demand is there.

-------------------------------------------------------------------------------

**HOW MUCH OF THIS HAS BEEN RUN, and by what.** The corpus was validated with
REAL weights on 2026-08-23 — `BAAI/bge-small-en-v1.5` and
`BAAI/bge-base-en-v1.5` through transformers, CLS-pooled and L2-normalized,
which is the same arithmetic llama.cpp performs for a GGUF declaring
`pooling_type = CLS`. Both scored 5/5 top-1; word-overlap scored 2/5. So the
corpus is known-good and the threshold below is not a guess.

**The llama.cpp path itself was NOT executed there.** The machine this was
written on cannot run the pinned `llama-cpp-python` wheel at all — every
version sampled aborts the process with `0xc000001d` (illegal instruction) on
its AVX2-only CPU, before any model is touched. This test is therefore written
to run on a reviewer's machine and skipped on that one. It is a real test, not
a placeholder: the skip is a hardware fact, and the assertions are the ones
that were validated against real weights through a different runtime.
"""
import os
import re

import pytest

#: The default curated model, which is what a reviewer following `TESTING.md`
#: will already have on disk. Overridable so the same test can be pointed at
#: another curated id without editing it.
MODEL_REPO = os.environ.get(
    "FUSED_TEST_EMBED_REPO", "nomic-ai/nomic-embed-text-v1.5-GGUF")
MODEL_FILE = os.environ.get(
    "FUSED_TEST_EMBED_FILE", "nomic-embed-text-v1.5.Q8_0.gguf")

#: Five passages with nothing in common but being English sentences. Short,
#: because the point is the semantics and not the chunking; from five
#: unrelated domains, because a corpus whose passages are near neighbours
#: tests the encoder's precision rather than whether it works at all.
CORPUS = [
    "The landlord is responsible for repairing a leaking roof within 14 days "
    "of written notice.",
    "Espresso should be pulled at roughly nine bars of pressure for 25 to 30 "
    "seconds.",
    "Photosynthesis converts light energy into chemical energy stored as "
    "glucose in plant cells.",
    "A pull request must be approved by one reviewer before it can be merged "
    "to the main branch.",
    "Sourdough starter needs feeding with equal parts flour and water every "
    "twelve hours.",
]

#: …and a paraphrase of each, written in the words someone would actually
#: type rather than in the passage's own vocabulary. The index is the passage
#: that answers it.
QUERIES = [
    ("water is coming through my ceiling when it rains", 0),
    ("how long should I run the coffee machine shot", 1),
    ("how do plants make food from sunlight", 2),
    ("who has to sign off before my code lands", 3),
    ("keeping the bread culture alive", 4),
]


def _words(text):
    return set(re.findall(r"[a-z]+", text.lower()))


def test_the_corpus_defeats_keyword_matching():
    """**The test that makes the next one mean something.** A retrieval test
    whose queries share their answer's vocabulary proves nothing an `in`
    operator could not do, and it would keep passing after a change that
    broke the encoder entirely.

    Word-overlap ranking scores 2 of 5 here, and even that overstates it: one
    of the two is a coincidence, matched on the stopwords "before" and "to".
    Two queries share NO word at all with the passage that answers them.

    Asserted as an upper bound rather than an exact number so that rewording
    a passage for clarity does not fail the build — but it must stay well
    under the 5/5 the encoder is required to reach below, or the two tests
    stop being distinguishable.
    """
    picks = [
        max(range(len(CORPUS)), key=lambda i: len(_words(query) & _words(CORPUS[i])))
        for query, _want in QUERIES
    ]
    hits = sum(1 for (_q, want), got in zip(QUERIES, picks) if want == got)
    assert hits <= 2, (
        f"word overlap now answers {hits}/{len(QUERIES)} of these queries, so "
        f"the retrieval test below no longer distinguishes a working encoder "
        f"from a lookup table — reword the queries away from their passages' "
        f"vocabulary")
    # …and at least two queries must share literally nothing with their
    # answer, which is the property that cannot be reached by luck.
    zero_overlap = sum(
        1 for query, want in QUERIES if not (_words(query) & _words(CORPUS[want])))
    assert zero_overlap >= 2, zero_overlap


@pytest.fixture(scope="module")
def _llm():
    """A real llama.cpp embedding model, or a skip that says which demand
    failed.

    Three separate demands, skipped separately because they fail for
    different reasons and a reader deserves to know which:

    1. `llama_cpp` importable — the runner venv is opt-in and is not what
       this suite installs (AI-11c).
    2. The GGUF already in the Hub cache. **Never downloaded by this test**:
       a test suite that fetches 146MB the first time it runs is a test suite
       people disable. `TESTING.md` says how to put it there in one command.
    3. `Llama(...)` actually constructing. This one is not paranoia — the
       maintainer's pinned wheel aborts the whole PROCESS with an illegal
       instruction on a CPU without the instruction set it was built
       against, so on such a machine there is nothing to catch and the skip
       cannot help. What it DOES catch is the milder failures: a corrupt
       download, a Vulkan build with no loader.
    """
    llama_cpp = pytest.importorskip(
        "llama_cpp", reason="the llamacpp-embed runner venv is opt-in (AI-11c)")

    from huggingface_hub import try_to_load_from_cache

    path = try_to_load_from_cache(MODEL_REPO, MODEL_FILE)
    if not isinstance(path, str):
        pytest.skip(
            f"{MODEL_REPO}/{MODEL_FILE} is not in the Hub cache, and this test "
            f"will not download it — see TESTING.md")

    try:
        return llama_cpp.Llama(model_path=path, embedding=True, n_ctx=2048,
                               n_batch=2048, n_ubatch=2048, verbose=False)
    except Exception as error:  # noqa: BLE001 - a load failure here is a fact
                                # about this machine, not a result
        pytest.skip(f"llama.cpp could not load {MODEL_FILE}: {error}")


def _embed(llm, texts, kind):
    """The runner's own pipeline, reached through its own modules.

    Deliberately NOT a reimplementation: `text_embed_common.prompted` and
    `unit_normalize` are the functions the worker calls, so this test
    exercises the prompt table and the normalization that actually ship. Only
    the `Llama` handle is supplied from outside, because the worker's `load`
    is what this fixture is standing in for.
    """
    from fused_render.ai.runners import formats, text_embed_common

    scheme = formats.text_embed_scheme(MODEL_REPO, MODEL_FILE)
    return text_embed_common.unit_normalize(
        llm.embed(text_embed_common.prompted(texts, kind, scheme)))


def _dot(a, b):
    """Cosine similarity as a PLAIN DOT PRODUCT, with no magnitudes divided
    out — which is legal here only because the vectors are unit length, and
    is therefore also a check on that guarantee."""
    return sum(x * y for x, y in zip(a, b))


def test_a_paraphrase_finds_its_passage(_llm):
    """**The point of the whole capability.** Each query must rank its own
    passage first, against a corpus its words barely touch.

    Query and document are embedded with DIFFERENT `kind` values, which is
    how a page is meant to use this and therefore how it should be tested —
    testing the symmetric path would leave the asymmetric one, the one the
    endpoint exists to offer, unexercised.

    5/5 rather than a softer threshold because 5/5 is what real weights
    scored when this corpus was validated (see the module docstring). A
    threshold below what the thing actually does is a threshold that lets a
    regression through.
    """
    documents = _embed(_llm, CORPUS, "document")
    queries = _embed(_llm, [q for q, _w in QUERIES], "query")

    misses = []
    for (text, want), vector in zip(QUERIES, queries):
        scores = [_dot(vector, doc) for doc in documents]
        got = max(range(len(scores)), key=scores.__getitem__)
        if got != want:
            misses.append((text, want, got, [round(s, 3) for s in scores]))

    assert not misses, misses


def test_the_vectors_are_unit_length_and_the_right_width(_llm):
    """The guarantee `fused.ai.embedText` publishes and pages are told to
    rely on: cosine is a plain dot product, so a vector's dot with ITSELF is
    1. Checked against a real model rather than a fake, because
    `unit_normalize` running is not the same fact as the runner calling it.
    """
    vectors = _embed(_llm, CORPUS, "document")
    dims = {len(v) for v in vectors}
    assert len(dims) == 1, dims
    for vector in vectors:
        assert abs(_dot(vector, vector) - 1.0) < 1e-6


def test_an_unrelated_query_scores_lower_than_a_related_one(_llm):
    """Similarity has to be ORDERED, not merely computed. A degenerate
    encoder — one that returns nearly the same vector for everything, which
    is exactly what a wrongly-pooled or wrongly-loaded model does — still
    passes the two tests above by luck of argmax ties; it cannot pass this,
    because the gap it asserts is a real separation rather than a ranking.
    """
    documents = _embed(_llm, CORPUS, "document")
    related, unrelated = _embed(
        _llm, ["my roof is leaking after the storm",
               "the mitochondrion is the powerhouse of the cell"], "query")
    assert _dot(related, documents[0]) > _dot(unrelated, documents[0]) + 0.05
