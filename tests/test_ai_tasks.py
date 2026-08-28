"""The task vocabulary: every Hub `pipeline_tag`, and whether we run it.

The invariant this file exists for is not "the table is right" — it is that the
table is TOTAL and its three states stay distinguishable. Two bugs came from the
old shape and each has a test here:

* an unrecognised tag became a prose label nobody had classified, and a format
  guess then filed it under a capability (`SymphonyGen/SymphonyGen`);
* "ruled out" and "never heard of" were the same `None`, so no caller could
  refuse to rescue the first.
"""

import pytest

from fused_render.ai import registry, tasks


def test_every_vendored_tag_is_classified():
    """The completeness rule, restated for a CLOSED vocabulary.

    Its predecessor enumerated the labels the listing's own tables could emit
    and required each to be classified — which was sound until the tag-to-label
    step grew a passthrough, at which point the enumeration was a fiction and
    the test passed while `reinforcement-learning` walked through it. Keyed on
    the vendored table instead: every row is either served by a capability or
    carries a reason it is not, and there is no third way to be in this table.
    """
    for tag, task in tasks.TASKS.items():
        reading = tasks.classify(tag)
        assert reading.support in (tasks.SUPPORTED, tasks.NO_RUNNER), tag
        assert (reading.capability is not None) == reading.supported, tag
        if not reading.supported:
            assert reading.reason, tag


def test_a_tag_this_build_has_never_heard_of_is_unknown_not_text():
    """The Hub adds tags; this table is a snapshot. What must NOT happen is the
    old behaviour — an unvendored tag turning into a prose label that later code
    treats as a classification. It is `UNKNOWN`, it names no capability, and it
    is still printable so a card can show what the author wrote."""
    reading = tasks.classify("holographic-telepathy")
    assert reading.support == tasks.UNKNOWN
    assert reading.capability is None
    assert reading.label == "holographic telepathy"
    assert not reading.ruled_out


def test_no_evidence_at_all_is_not_a_task():
    assert tasks.classify(None) == tasks.NOTHING
    assert tasks.classify("   ") == tasks.NOTHING
    assert tasks.NOTHING.capability is None
    # No sentence: "we cannot tell what this is" is said by the absent label,
    # and a reason here would be a claim we have not earned.
    assert tasks.NOTHING.reason == ""


def test_ruled_out_and_unknown_are_tellable_apart():
    """The distinction the format fallbacks branch on (`hub_cache._engine`,
    `cached_capability`). Both answer `capability is None`; only one of them may
    be rescued by what the weight files look like."""
    assert tasks.classify("text-to-speech").ruled_out
    assert not tasks.classify("nonsense-tag").ruled_out


@pytest.mark.parametrize("tag, capability", [
    ("text-generation", registry.TEXT_GENERATION),
    # A vision-language checkpoint IS the causal LM the text runner loads when
    # you only give it text — every entry in this app's own MLX catalog carries
    # this tag, and leaving it unmapped once took the Load button off them.
    ("image-text-to-text", registry.TEXT_GENERATION),
    # The same checkpoints, the Hub's OTHER tag for asking about a picture —
    # mlx-vlm answers this exactly as it answers "image + text to text": one
    # runner, one capability, no separate VQA-only model exists to need a
    # runner of its own.
    ("visual-question-answering", registry.TEXT_GENERATION),
    ("text-to-image", registry.IMAGE_GENERATION),
    ("automatic-speech-recognition", registry.SPEECH_TO_TEXT),
    # The tag a SigLIP or CLIP repo actually carries: a dual encoder, named
    # after one thing you can do with its two towers.
    ("zero-shot-image-classification", registry.EMBEDDINGS),
    # …and the two a PROSE encoder carries. Three tags onto one capability,
    # which is unique to this row — see `registry.EMBEDDINGS`'s docstring.
    ("feature-extraction", registry.EMBEDDINGS),
    ("sentence-similarity", registry.EMBEDDINGS),
    # `ltx-video` serves this one prompt-only; its image-conditioned siblings
    # (image-to-video, image-text-to-video) stay unmapped below.
    ("text-to-video", registry.VIDEO_GENERATION),
])
def test_the_tags_a_runner_serves(tag, capability):
    assert tasks.classify(tag).capability == capability


def test_the_IMAGE_only_encoder_tag_is_still_deliberately_unserved():
    """`image-feature-extraction` did NOT move when its two text neighbours did,
    and the difference is a tower rather than a policy.

    `feature-extraction` and `sentence-similarity` wear a text encoder, which
    both embedding runners now load and pool. This tag wears an IMAGE-ONLY one —
    DINOv2 and DINOv3 are what people download for it — and that has no text
    tower at all: the dual path wants both towers and the prose path wants a
    tokenizer, so neither can open it. A third model shape, not a use case
    nobody got to.
    """
    reading = tasks.classify("image-feature-extraction")
    assert reading.ruled_out
    assert "text tower" in reading.reason


def test_the_two_text_embedding_tags_carry_no_withheld_reason_any_more():
    """The other half: a SUPPORTED tag must not keep the excuse it had while it
    was unsupported. `test_a_supported_task_carries_no_excuse` enforces that
    generally; these two are named because their reason strings had been there
    long enough to read as permanent, and the comment above them in `tasks.py`
    predicted this move for just as long."""
    for tag in ("feature-extraction", "sentence-similarity"):
        reading = tasks.classify(tag)
        assert reading.supported, tag
        assert not reading.ruled_out, tag
        assert reading.capability == registry.EMBEDDINGS, tag


@pytest.mark.parametrize("tag", [
    "image-to-video", "text-to-speech", "text-to-audio",
    "reinforcement-learning", "robotics", "tabular-regression", "any-to-any",
])
def test_a_task_we_do_not_serve_says_so_in_words(tag):
    """The state the API layer asked for by name: not a silent null, a sentence.

    Every one of these is a real job someone will download a model for, and the
    page's answer has to be "this app does not run that" rather than a card with
    a missing button."""
    reading = tasks.classify(tag)
    assert reading.support == tasks.NO_RUNNER
    assert reading.label and reading.reason.endswith(".")


def test_a_supported_task_carries_no_excuse():
    for tag in tasks.supported_tags():
        assert tasks.classify(tag).reason == ""


def test_the_glossary_covers_everything_the_menu_offers():
    """HS-7: one vocabulary across both faces. A filter the Discover tab offers
    is a term someone has to understand, and the sentence is keyed by TAG now —
    the old label key is how one concept read from a card and from a config
    produced two spellings, only one of which had an entry."""
    for tag in tasks.supported_tags():
        assert tasks.help_for(tag), tag
    assert tasks.help_for("holographic-telepathy") is None
    assert tasks.help_for(None) is None


def test_the_table_is_a_faithful_snapshot_of_the_hubs_vocabulary():
    """Vendored from `@huggingface/tasks` (`packages/tasks/src/pipelines.ts`),
    which is the only authoritative enumeration — no Python package ships it.

    Pinned by SHAPE rather than by a fetch: this suite runs offline, and a test
    that went to the network to check a table would fail on a plane and pass in
    CI for the wrong reasons. Spot-checks are the tags whose absence would break
    a specific surface, plus the two the Hub retired (`text2text-generation`,
    `conversational`), which must not creep back in — a filter for either
    returns zero models today, verified against the live Hub 2026-08-23.
    """
    assert len(tasks.TASKS) == len(tasks.TAG_ORDER) == 57
    assert all(tag == tag.lower() and " " not in tag for tag in tasks.TASKS)
    for retired in ("text2text-generation", "conversational"):
        assert retired not in tasks.TASKS
    for present in ("summarization", "translation", "other", "robotics",
                    "image-text-to-image", "video-text-to-text"):
        assert present in tasks.TASKS


def test_supported_tags_is_derived_and_ordered():
    """Menu order comes from the table, and membership from the one `capability`
    field — the two hand-maintained lists this replaced could drift, and the
    drift was invisible until someone downloaded 8GB of something unloadable."""
    supported = tasks.supported_tags()
    assert supported == tuple(t for t in tasks.TAG_ORDER if tasks.TASKS[t].capability)
    assert set(supported) == {t for t in tasks.TASKS if tasks.TASKS[t].capability}
    # Text first: the tab opens on it, and a menu ordered by an enum's accident
    # reads as random.
    assert supported[0] == "text-generation"
