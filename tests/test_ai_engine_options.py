"""What an engine cannot do, refused in one place (`runners/engine_options.py`).

The module exists because two very different readers ask the same question: the
image/transcribe endpoints, deciding whether to open a job at all, and the
worker, deciding whether to decode/render. What is pinned here is the
SHARED-ness and the rule's shape — that a refusal is a refusal in both doors
and that the sentence is one sentence, not two that can drift.

`UNSUPPORTED` was empty for TRANSCRIBE options from D406 (which withdrew the
one engine, `parakeet-mlx`, that ever populated it for that call — both
remaining speech-to-text runners, `mlx-whisper` and `faster-whisper`, answer
every option `fused.ai.transcribe` takes) until the mflux-only base-image edit
option gave it its first real rows: the three diffusers image codes each
refuse `image`, because the diffusers pipeline's own editing signature is
unverified on any machine this app has run on. What is pinned here now is
that the table stays VALID and the mechanism stays LIVE — for a table with
real rows in it, not an empty one kept warm for a hypothetical.

Read alongside `tests/test_ai_runtime.py` (the endpoint's half).
"""
import importlib.util
import os

import pytest

from fused_render.ai import registry
from fused_render.ai.runners import engine_options

OPTIONS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "ai", "runners", "engine_options.py",
)


def test_every_runner_code_named_here_is_a_registered_runner():
    """The codes are bare strings, because this module is imported by
    interpreters that have no `fused_render` on their path. This is the check
    that buys back what the import would have given: a runner renamed in the
    registry and not here would silently stop being refused anything, and every
    option it cannot honour would be accepted and quietly ignored.

    Trivially true while the table is empty (D406) — kept so a future entry is
    checked the moment it is added, rather than the first time a runner is
    renamed after that."""
    codes = {runner.code for runner in registry.all_runners()}
    assert set(engine_options.UNSUPPORTED) <= codes


def test_the_table_holds_exactly_the_diffusers_image_refusal():
    """Pinned explicitly rather than left implicit: three rows, one per
    diffusers image code, all refusing `image` and nothing else — the state
    since the mflux-only base-image edit option shipped. If this fails
    because something else was added, that is fine — update this test
    alongside the entry."""
    assert set(engine_options.UNSUPPORTED) == {
        "diffusers-image", "diffusers-image-cuda", "diffusers-image-rocm"}
    for code, rules in engine_options.UNSUPPORTED.items():
        assert set(rules) == {"image"}, code


def test_the_three_diffusers_codes_carry_the_IDENTICAL_sentence():
    """A hardware variant reads the same pipeline class as the CPU row and
    would answer `image` identically if it were ever wired up — the fact is
    about the LIBRARY, not the wheel, so the three rows must not drift into
    three different sentences over time."""
    sentences = {rules["image"] for rules in engine_options.UNSUPPORTED.values()}
    assert len(sentences) == 1


def test_an_engine_with_nothing_to_refuse_refuses_nothing():
    """The common case, and the honest default for a code this table has never
    heard of: an exception list says nothing about what is not in it. Every
    speech-to-text engine currently falls in this bucket, and so does mflux —
    the one image engine that DOES honour `image`."""
    assert engine_options.unsupported_or_raise(
        "mlx-whisper", task="translate", language="en",
        initial_prompt="Acme Corp") is None
    assert engine_options.unsupported_or_raise(
        "faster-whisper", task="translate", language="en",
        initial_prompt="Acme Corp") is None
    assert engine_options.unsupported_or_raise(
        "some-future-runner", task="translate") is None
    assert engine_options.unsupported_or_raise(
        "mflux-image", image="/tmp/base.png") is None


@pytest.mark.parametrize("code", [
    "diffusers-image", "diffusers-image-cuda", "diffusers-image-rocm"])
def test_diffusers_image_refuses_the_edit_option(code):
    """The real entry, not a fake one: every diffusers image code — CPU and
    both hardware variants — refuses `image` with a sentence naming the way
    out (the mflux engine, on the Engines tab)."""
    with pytest.raises(ValueError, match="Diffusers image engine"):
        engine_options.unsupported_or_raise(code, image="/tmp/base.png")
    with pytest.raises(ValueError, match="Engines tab"):
        engine_options.unsupported_or_raise(code, image="/tmp/base.png")
    # Absent `image` is an ordinary prompt-only render — never refused.
    assert engine_options.unsupported_or_raise(code, image=None) is None


def test_the_module_reads_the_same_BY_PATH_as_it_does_through_the_package(monkeypatch):
    """Two loaders, one module. The runner imports it by path off `sys.path`
    (its own interpreter has no `fused_render`); the server imports it as
    `fused_render.ai.runners.engine_options`. A module that resolved under only
    one of them would be half a rule, and the failure would land in production
    rather than here.

    Exercised with a monkeypatched entry rather than the bare empty-table
    comparison this reduced to after D406: `{} == {}` passes no matter how the
    two loads diverge, so a copy that behaved differently — the exact drift
    this test exists to catch — would pass silently right alongside it. The
    fake entry is set on BOTH loads and both are made to actually raise, so a
    divergent `unsupported_or_raise` (wrong signature, wrong lookup, a stale
    duplicate file reachable by path) fails here rather than in production."""
    spec = importlib.util.spec_from_file_location("runners_engine_options",
                                                  OPTIONS_PATH)
    assert spec is not None and spec.loader is not None, OPTIONS_PATH
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setitem(module.UNSUPPORTED, "fake-refusing-engine",
                        {"task": "the fake engine has no translate task"})
    monkeypatch.setitem(engine_options.UNSUPPORTED, "fake-refusing-engine",
                        {"task": "the fake engine has no translate task"})

    with pytest.raises(ValueError, match="no translate task"):
        module.unsupported_or_raise("fake-refusing-engine", task="translate")
    with pytest.raises(ValueError, match="no translate task"):
        engine_options.unsupported_or_raise("fake-refusing-engine", task="translate")

    assert module.UNSUPPORTED == engine_options.UNSUPPORTED
    assert module.unsupported_or_raise("mlx-whisper", task="translate") is None
