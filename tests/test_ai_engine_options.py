"""What an engine cannot do, refused in one place (`runners/engine_options.py`).

The module exists because two very different readers ask the same question: the
transcribe endpoint, deciding whether to open a job at all, and the worker,
deciding whether to decode. What is pinned here is the SHARED-ness and the
rule's shape — that a refusal is a refusal in both doors and that the sentence
is one sentence, not two that can drift.

`UNSUPPORTED` is empty as of D406, which withdrew the one engine
(`parakeet-mlx`) that ever populated it — both remaining speech-to-text
runners, `mlx-whisper` and `faster-whisper`, answer every option
`fused.ai.transcribe` takes. What is pinned here now is that the table stays
VALID and the mechanism stays LIVE with nothing in it, rather than either
being deleted along with the one engine that used it.

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


def test_the_table_is_empty_now_that_no_engine_refuses_anything():
    """Pinned explicitly rather than left implicit: an empty `UNSUPPORTED` is
    the correct state since D406 withdrew `parakeet-mlx`, not a regression
    waiting to be noticed. If this fails because something was added, that is
    fine — update this test alongside the entry."""
    assert engine_options.UNSUPPORTED == {}


def test_an_engine_with_nothing_to_refuse_refuses_nothing():
    """The common case, and the honest default for a code this table has never
    heard of: an exception list says nothing about what is not in it. Every
    registered engine currently falls in this bucket."""
    assert engine_options.unsupported_or_raise(
        "mlx-whisper", task="translate", language="en",
        initial_prompt="Acme Corp") is None
    assert engine_options.unsupported_or_raise(
        "faster-whisper", task="translate", language="en",
        initial_prompt="Acme Corp") is None
    assert engine_options.unsupported_or_raise(
        "some-future-runner", task="translate") is None


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
