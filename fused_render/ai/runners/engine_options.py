"""Options an ENGINE cannot honour, and the sentence each is refused with.

`fused.ai.transcribe` takes `task`, `language` and `initialPrompt`, and the two
whisper engines answer all three. Parakeet answers none of them (D319, SPEC
AI-10g): it transcribes only, it detects its own language among the 25 its
weights were trained on and cannot be pinned to one, and a transducer has no
text to condition on. **Refused, never ignored** — accepting an option and
quietly doing something else is the failure this app rates worst, because a
page that asked for English and got French has nothing on the row telling it
which engine decided.

**`words` (D392) does NOT belong in this table**, and it is the one option that
does not: an ignored option is refused here because it is undetectable, and word
timings are detectable — honouring them puts a `words` list on the segment and
declining leaves the key off, so a caller reads which happened off the reply.
It is answered best-effort in `mlx_whisper/worker.py`, where the decline lives.

**This module is where that rule lives, once**, for exactly the reason
`diarize.speakers_or_raise` sits where it does: the refusal has to happen in
TWO places and must be one sentence. The endpoint refuses first, before a job
row opens and before a multi-gigabyte model downloads — the runner is already
resolved server-side (`registry.for_capability`), so the answer is available
immediately and making the user wait for a load to be told "no" is a cost with
no benefit. The worker refuses again on arrival, because the bridge and the
endpoint are not the only doors into that process.

**Stdlib only, and no import of `fused_render`.** The same constraint
`formats.py`, `diarize.py`, `vad.py` and `partial.py` document: it is read by a
runner on its own interpreter, with the app's package deliberately off its
path, and by the server as `fused_render.ai.runners.engine_options`.

Runner CODES appear as bare strings for that same reason, and
`tests/test_ai_engine_options.py` asserts every one of them is a registered
runner — the drift a missing import would otherwise invite.
"""

from __future__ import annotations

#: The task every engine here does. The one option whose refusal is about a
#: VALUE rather than about presence: `task: "transcribe"` is sent on every
#: request, so a check on presence would refuse them all.
TRANSCRIBE = "transcribe"

#: Runner code -> option name -> why this engine cannot do it.
#:
#: An engine with nothing to refuse is ABSENT rather than mapped to an empty
#: dict, so the common case costs a dict lookup and the table reads as the
#: exception list it is. Each sentence names the ENGINE and the way out,
#: because the page is usually correct and simply resolved to a runner it was
#: not written for — which since D302 is a choice its user made on the Engines
#: tab, and one they can unmake there.
#:
#: **Keyed by CODE, so a HARDWARE VARIANT is a separate key.** The per-hardware
#: torch rows (`transformers-text-cuda`, `diffusers-image-rocm`, …) need no entry
#: and have none: they answer exactly what their CPU sibling answers, which for
#: the torch runners is every option here, and the CPU rows are absent too. But
#: the day a runner that DOES refuse something gains a CUDA or ROCm variant, the
#: variant needs its own entry — an unknown code refuses nothing (see
#: `unsupported_or_raise`), so the failure would be an accepted-and-ignored
#: option, which is the outcome this module exists to make impossible.
UNSUPPORTED = {
    "parakeet-mlx": {
        "task": (
            "the Parakeet engine only transcribes — it has no translate task. "
            "Ask for task: 'transcribe', or switch this capability to a "
            "Whisper engine on the AI Models page, which translates into "
            "English."),
        "language": (
            "the Parakeet engine has no 'language' option — it detects the "
            "language itself (25 European languages on parakeet-tdt-0.6b-v3) "
            "and cannot be pinned to one. Drop the option, or switch this "
            "capability to a Whisper engine on the AI Models page."),
        "initialPrompt": (
            "the Parakeet engine has no 'initialPrompt' — a transducer decodes "
            "audio with no text to condition on, so names and jargon cannot be "
            "hinted the way they can on Whisper. Drop the option, or switch "
            "this capability to a Whisper engine on the AI Models page."),
    },
}


def unsupported_or_raise(runner_code, *, task=None, language=None,
                         initial_prompt=None):
    """`ValueError` if `runner_code` cannot honour one of these, else None.

    Named arguments rather than the request dict, because the two callers hold
    the values in different shapes — the endpoint has a JSON body, the worker
    has the fields it already normalised — and a function that took the body
    would make the wire format part of this rule.

    An unknown or absent runner code refuses NOTHING, which is the honest
    default: this table is an exception list, and a code it has never heard of
    is an engine with nothing to say rather than an engine to distrust.

    `task` is refused on its VALUE and the other two on their PRESENCE. That
    asymmetry is the request's, not this module's: every transcribe request
    carries `task: "transcribe"` explicitly, while `language` and
    `initialPrompt` arrive as None or an empty string unless somebody asked for
    them.
    """
    rules = UNSUPPORTED.get(runner_code)
    if not rules:
        return None
    if task and task != TRANSCRIBE and "task" in rules:
        raise ValueError(rules["task"])
    if language and "language" in rules:
        raise ValueError(rules["language"])
    if initial_prompt and "initialPrompt" in rules:
        raise ValueError(rules["initialPrompt"])
    return None
