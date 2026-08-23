"""Validating and shaping one `fused.ai.embedText()` request, written once
(SPEC §40).

`llama_embed` (two folders) and `mlx_text_embed` are three environments
serving ONE capability, and — unlike the `embeddings` pair that
`embed_common.py` serves — they do NOT read the same repos: llama.cpp opens a
GGUF and mlx-embeddings opens a directory of safetensors, exactly the split
`llama_text`/`mlx_text` already have for chat. What they must still agree on
is the two things a caller can observe: whether a request is well-formed, and
what a returned vector MEANS. This module is where both live, so neither side
can drift on them.

**Why this is a second module beside `embed_common.py` rather than three more
functions inside it.** That file's `request_kind` accepts `paths` — an image
per item — because the capability it serves is a DUAL ENCODER, where a
picture and a sentence land in one space and comparing them is the entire
point. Nothing here has a vision tower. Folding both request shapes into one
validator would have produced a function whose contract was "exactly one of
texts/paths, unless the caller is the text-embedding capability, in which
case paths is an error" — a shape that is one edit away from letting a
`paths` request through to a model that would then embed the FILENAMES as
prose and return perfectly plausible, perfectly wrong vectors. Two validators
that each refuse the other's input by name is the version of that with no
silent failure in it.

`unit_normalize` IS imported from `embed_common`, because that one is not a
request-shape question: it is the arithmetic that makes a cosine similarity a
plain dot product, and it must be bit-for-bit the same rule in both
capabilities or two vectors from two engines mean different things.

**Mostly stdlib, and no import of `fused_render`** — the constraint
`formats.py` and `embed_common.py` both document, for the same reason: this
is imported by three runners' own interpreters through the same `sys.path`
insert that reaches `worker_base`, and none of them has this app on its path.
"""

from __future__ import annotations

import embed_common
import formats

#: The batch ceiling, taken from `embed_common` rather than restated. The
#: number is not a fact about dual encoders — it is a fact about how much
#: work one HTTP request should do before a caller would rather see progress
#: — so the two capabilities answering differently would be an inconsistency
#: with no reason behind it.
MAX_ITEMS = embed_common.MAX_ITEMS

#: The two things a text can BE to a retrieval model.
KINDS = formats.TEXT_EMBED_KINDS

#: What a caller who says nothing gets.
#:
#: **"document", and it is the one defaulting decision in this file that
#: could quietly cost someone their recall, so here is the whole argument.**
#:
#: There is no neutral option. Every scheme in `formats.TEXT_EMBED_PROMPTS`
#: has two sides and a call must pick one, so the question is only which
#: wrong guess is cheaper when the caller has not thought about it yet.
#:
#: * Default "query", and someone who indexes a corpus with a bare call
#:   stamps the query instruction onto ten thousand passages. Every later
#:   search then compares a properly-prefixed query against a corpus that
#:   claims to be ten thousand queries — the mismatched state, which is
#:   measurably worse than using no prefix at all (the e5 card says so
#:   outright about its own pair).
#: * Default "document", and the same person gets a corpus that is internally
#:   consistent. If they never discover `kind`, every text in the system —
#:   corpus and query alike — carries the same prefix, which is the SYMMETRIC
#:   behaviour every one of these models supports and the behaviour every
#:   encoder had before asymmetric prompting existed. It is not optimal; it
#:   degrades gracefully rather than to the mismatched state.
#:
#: The tie-breaker is `bge`: its document side is the EMPTY string, because
#: its card instructs the query only. So on that family this default means a
#: bare call embeds text verbatim — precisely what someone who has not read
#: about prompt schemes expects to happen — while the opposite default would
#: silently prepend a sentence about searching to text nobody was searching.
#:
#: Documented on the bridge (`fused.ai.embedText`) as well, because a default
#: whose reasoning lives only in the runner is a default page authors cannot
#: act on.
DEFAULT_KIND = formats.TEXT_EMBED_DEFAULT_KIND

#: What to say when a caller passes `paths` (or `images`) to an endpoint with
#: no vision tower behind it. **Named, not a generic "unexpected field".** The
#: request is not a typo — it is someone who found `fused.ai.embed`'s image
#: half and reasonably assumed this endpoint had one, so the sentence has to
#: say which endpoint DOES and not merely that this one does not.
_NO_IMAGES = (
    "'{field}' is not something a text embedding model can read — this "
    "capability loads a text encoder, which has no vision tower and no image "
    "input at all. Passing image paths here would embed the FILENAMES as "
    "prose and return vectors that look fine and mean nothing. For text and "
    "images in one comparable space, use fused.ai.embed() instead, which "
    "loads a dual encoder (SigLIP/CLIP) for exactly that."
)


def request_texts(body: dict) -> tuple[list, str]:
    """`(texts, kind)`, or raises `ValueError` naming what is wrong.

    The one shape all three engines accept: a non-empty `texts` list of
    non-empty strings, at most `MAX_ITEMS` long, and an optional `kind` that
    must be one of `KINDS` when present.

    Checked here so a batch of 65 does not read as a llama.cpp limit on one
    engine and an MLX one on another — the same argument `embed_common`'s
    own `request_kind` makes about the pair it serves — and checked AGAIN by
    the route before a model is even resolved, so a malformed request costs
    nothing rather than a 409 that implies the fix is to wait.

    **An unrecognised `kind` is refused rather than defaulted.** It is the
    one field here whose wrong value produces no error anywhere downstream:
    a `kind: "queries"` that fell through to `DEFAULT_KIND` would return
    unit-length vectors of the right dimension, computed against the wrong
    half of the prompt pair, and nothing the caller could measure would say
    so. Refusing costs a typo one round trip; defaulting costs a silent
    accuracy regression nobody attributes to this call.
    """
    for field in ("paths", "images"):
        if body.get(field):
            raise ValueError(_NO_IMAGES.format(field=field))

    texts = body.get("texts")
    if not isinstance(texts, list) or not texts:
        raise ValueError("pass 'texts' — a non-empty list of strings")
    if len(texts) > MAX_ITEMS:
        raise ValueError(f"at most {MAX_ITEMS} items at a time, got {len(texts)}")
    for index, item in enumerate(texts):
        if not isinstance(item, str) or not item:
            raise ValueError(f"'texts[{index}]' must be a non-empty string")

    kind = body.get("kind")
    if kind is None:
        kind = DEFAULT_KIND
    if kind not in KINDS:
        raise ValueError(
            f"'kind' must be one of {', '.join(repr(k) for k in KINDS)} "
            f"(got {kind!r}) — a retrieval model instructs a question and a "
            f"passage differently, so this picks which prompt goes in front "
            f"of these texts. Leave it out for {DEFAULT_KIND!r}.")
    return texts, kind


def prompted(texts: list, kind: str, scheme: str) -> list:
    """`texts` with this model's prefix for `kind` glued to the front of each.

    A plain concatenation and nothing cleverer, because every scheme in
    `formats.TEXT_EMBED_PROMPTS` is literally a string the model's own card
    puts in front of the text. The `"none"` scheme's prefix is `""`, so this
    returns the input unchanged for a model with no convention — the same
    list, not a copy, is deliberately NOT relied on: the caller may hold the
    original for its own reporting.
    """
    prefix = formats.text_embed_prompt(scheme, kind)
    if not prefix:
        return list(texts)
    return [prefix + text for text in texts]


def unit_normalize(vectors: list) -> list:
    """Every row scaled to length 1, in plain Python floats.

    `embed_common`'s function, re-exported under this module's name rather
    than imported directly by each worker. The indirection is worth one line:
    it means every runner in THIS capability reaches its normalization
    through the module that documents this capability's contract, so the
    guarantee `/api/ai/embed-text` publishes — a cosine similarity is a plain
    dot product — has one place to be read about.

    **Applied even when the model already normalized.** llama.cpp normalizes
    when the GGUF declares a pooling type, and `mlx-embeddings` normalizes
    inside every `Model.__call__` this app reaches. Re-normalizing an
    already-unit vector is a no-op to within float error, and paying for that
    no-op is much cheaper than the alternative: a future engine, a future
    pooling type, or an upstream that quietly stops normalizing would
    otherwise break the one promise callers were told to rely on, and would
    break it silently.
    """
    return embed_common.unit_normalize(vectors)
