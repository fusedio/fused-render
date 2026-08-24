"""Drift pin: the Benchmark tab's `workloadNote` (frontend/src/apps/ai_models/
lib/benchmark.ts) against the server's own `WORKLOADS` table
(fused_render/ai/benchmark.py), in the spirit of D470's
`_IMAGE_WIRE_KEYS`/`_TRANSCRIBE_WIRE_KEYS` pin (test_fused_ai_client.py).

The facts a reader needs — what a run actually measures — live exactly once,
in `WORKLOADS`, and D483 built the frontend's summary sentence FROM that
table (via a new `/api/ai/benchmark` field, `workloads`) rather than as a
second, hand-typed copy of the same params. A hand-typed copy is exactly
what drifts silently: someone adds a param to a workload server-side, the
frontend sentence never mentions it, and nobody notices because nothing
failed. This is what fails instead.

Cannot import the TypeScript module directly (no JS runtime in the Python
test suite), so this reads `benchmark.ts` as plain text and checks that
every param name `WORKLOADS` actually uses appears literally in the source
— the same "read the frontend file, don't render it" approach
`test_new_task_css.py` already uses for a stylesheet's own invariants.
"""
import os
import re

from fused_render.ai import benchmark

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FRONTEND_LIB = os.path.join(
    REPO_ROOT, "frontend", "src", "apps", "ai_models", "lib", "benchmark.ts",
)

#: Free-text CONTENT params that `workloadNote` deliberately never echoes
#: verbatim — a reader comparing two models needs to know the prompt/texts
#: are FIXED across runs, not read their literal words, which would turn one
#: reference line into a paragraph nobody is comparing against anyway. An
#: explicit, narrow exemption (the same shape `NO_WORKLOAD_YET` uses in
#: `benchmark.py` for the one capability with no workload at all), not a
#: silent skip — `test_the_content_exemption_is_narrow_and_still_true` below
#: keeps it honest in both directions.
_CONTENT_PARAMS_WITH_NO_LITERAL_ECHO = frozenset({"prompt", "texts"})


def _frontend_source() -> str:
    with open(_FRONTEND_LIB, encoding="utf-8") as f:
        return f.read()


def _workload_note_body(source: str) -> str:
    """Just the `workloadNote` function's own text, so a param name that
    happens to appear elsewhere in this large file (a metric key sharing a
    name, say) cannot make this test pass for the wrong reason."""
    match = re.search(
        r"export function workloadNote\([^)]*\)[^{]*\{(.*?)\n\}",
        source,
        re.S,
    )
    assert match, "workloadNote not found in benchmark.ts — has it been renamed or moved?"
    return match.group(1)


def test_every_workload_param_name_appears_in_the_frontends_note():
    """A param added to any `WORKLOADS` entry, server-side, must be named
    (not necessarily its VALUE — the key itself) somewhere in the sentence
    `workloadNote` builds for that capability, or a reader has no way to
    learn it exists short of reading Python source."""
    source = _frontend_source()
    body = _workload_note_body(source)
    missing = []
    for capability, workload in benchmark.WORKLOADS.items():
        for param_name in workload.params:
            if param_name in _CONTENT_PARAMS_WITH_NO_LITERAL_ECHO:
                continue
            if param_name not in body:
                missing.append(f"{capability}.{param_name}")
    assert not missing, (
        "workloadNote (frontend/src/apps/ai_models/lib/benchmark.ts) does not "
        f"mention these WORKLOADS params: {missing} — either render them in "
        "the note, or add them to _CONTENT_PARAMS_WITH_NO_LITERAL_ECHO with a "
        "reason if they are genuinely free-text content."
    )


def test_every_workload_capability_has_its_own_case():
    """A capability could satisfy the param-name check above by accident (a
    param name shared with a DIFFERENT capability's own case) unless each
    capability is also confirmed to have a `case "<capability>":` of its
    own inside `workloadNote`."""
    source = _frontend_source()
    body = _workload_note_body(source)
    for capability in benchmark.WORKLOADS:
        assert f'case "{capability}":' in body, (
            f"workloadNote has no case for {capability!r}, which has a real "
            "WORKLOADS entry — a reader benchmarking it gets no explanation "
            "of what the run measures."
        )


def test_the_content_exemption_is_narrow_and_still_true():
    """Both directions, the same discipline `NO_WORKLOAD_YET`'s own pair of
    tests hold it to: every exempted name must actually be a real param
    somewhere in `WORKLOADS` (or the exemption is protecting nothing and is
    stale), and the exemption must not have grown to cover a NUMERIC param
    that materially changes comparability (this test does not know which
    future params are numeric, so it pins today's two by name — a third
    exemption added later gets the same scrutiny in review, not a silent
    pass here)."""
    all_params = {
        name
        for workload in benchmark.WORKLOADS.values()
        for name in workload.params
    }
    assert _CONTENT_PARAMS_WITH_NO_LITERAL_ECHO <= all_params
    assert _CONTENT_PARAMS_WITH_NO_LITERAL_ECHO == {"prompt", "texts"}


def test_the_note_carries_the_workloads_own_name_and_revision():
    """A run's comparability depends on which workload NAME and REVISION
    produced it (the run archive already records both per run) — the note
    has to say so too, not just the params, since two readers could be
    looking at a `text-128-tokens` rev 1 result and a rev 2 one side by side
    with no way to tell from the note alone that they are not comparable."""
    source = _frontend_source()
    body = _workload_note_body(source)
    assert "workload.name" in body
    assert "workload.revision" in body
