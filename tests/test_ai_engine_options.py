"""What an engine cannot do, refused in one place (`runners/engine_options.py`).

The module exists because two very different readers ask the same question: the
transcribe endpoint, deciding whether to open a job at all, and the worker,
deciding whether to decode. What is pinned here is the SHARED-ness and the
rule's shape — that a refusal is a refusal in both doors and that the sentence
is one sentence, not two that can drift.

Read alongside `tests/test_ai_parakeet_worker.py` (the worker's half) and
`tests/test_ai_runtime.py` (the endpoint's).
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
    option it cannot honour would be accepted and quietly ignored."""
    codes = {runner.code for runner in registry.all_runners()}
    assert set(engine_options.UNSUPPORTED) <= codes


def test_an_engine_with_nothing_to_refuse_refuses_nothing():
    """The common case, and the honest default for a code this table has never
    heard of: an exception list says nothing about what is not in it."""
    assert engine_options.unsupported_or_raise(
        "mlx-whisper", task="translate", language="en",
        initial_prompt="Acme Corp") is None
    assert engine_options.unsupported_or_raise(
        "some-future-runner", task="translate") is None


@pytest.mark.parametrize("sent,needle", [
    ({"task": "translate"}, "only transcribes"),
    ({"language": "en"}, "'language' option"),
    ({"initial_prompt": "Acme Corp"}, "'initialPrompt'"),
])
def test_parakeet_refuses_what_it_cannot_do_and_names_the_way_out(sent, needle):
    with pytest.raises(ValueError) as raised:
        engine_options.unsupported_or_raise("parakeet-mlx", **sent)
    message = str(raised.value)
    assert needle in message
    # The engine and the remedy, both: the page is usually correct and simply
    # resolved to a runner it was not written for, which its user can change.
    assert "Parakeet" in message and "AI Models page" in message


def test_the_ORDINARY_request_every_page_sends_is_not_refused():
    """`task: "transcribe"` is on every request and `language`/`initialPrompt`
    arrive as None or "" unless somebody asked for them — a check on presence
    rather than on value would refuse every call this engine exists to serve."""
    for language in (None, ""):
        assert engine_options.unsupported_or_raise(
            "parakeet-mlx", task=engine_options.TRANSCRIBE, language=language,
            initial_prompt=language) is None


def test_the_module_reads_the_same_BY_PATH_as_it_does_through_the_package():
    """Two loaders, one module. The runner imports it by path off `sys.path`
    (its own interpreter has no `fused_render`); the server imports it as
    `fused_render.ai.runners.engine_options`. A module that resolved under only
    one of them would be half a rule, and the failure would land in production
    rather than here."""
    spec = importlib.util.spec_from_file_location("runners_engine_options",
                                                  OPTIONS_PATH)
    assert spec is not None and spec.loader is not None, OPTIONS_PATH
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.UNSUPPORTED == engine_options.UNSUPPORTED
    with pytest.raises(ValueError):
        module.unsupported_or_raise("parakeet-mlx", task="translate")


def test_the_worker_imports_the_ONE_implementation_rather_than_a_copy():
    """The structural half: a private table back inside the runner folder would
    fail no behavioural test, because both copies would pass their own — and
    the endpoint would then be refusing with yesterday's sentence."""
    runners = os.path.dirname(OPTIONS_PATH)
    source = open(os.path.join(runners, "parakeet_mlx", "worker.py"),
                  encoding="utf-8").read()
    assert "import engine_options" in source
