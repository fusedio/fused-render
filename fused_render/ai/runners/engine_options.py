"""Options an ENGINE cannot honour, and the sentence each is refused with.

`fused.ai.transcribe` takes `task`, `language` and `initialPrompt`, and the two
speech-to-text engines this app ships — MLX Whisper and Faster Whisper — both
answer all three, so `UNSUPPORTED` carried no TRANSCRIBE rows for a while. It
carried one entry, `parakeet-mlx` (D319, SPEC AI-10g), for the engine that
answered none of them: it transcribed only, detected its own language among
the 25 its weights were trained on with no way to pin one, and was a
transducer with no text to condition on. D406 withdrew that engine —
maintenance cost not justified by use — and the table went with it, but the
MECHANISM stayed: the day another engine refuses an option, this is where the
refusal is declared. That day came for `fused.ai.image`'s own `image` option
(D428) — every diffusers image code refuses it, mflux is the only engine that
honours it — so the table is no longer empty, and the mechanism is now proven
across BOTH job-backed AI calls, not just the one it was written for.
**Refused, never ignored** — accepting an option and quietly doing something
else is the failure this app rates worst, because a page that asked for
English and got French has nothing on the row telling it which engine
decided, and neither does a page that asked to edit a photo and got a fresh
one instead.

**`words` (D392) does NOT belong in this table**, and it is the one option that
does not: an ignored option is refused here because it is undetectable, and word
timings are detectable — honouring them puts a `words` list on the segment and
declining leaves the key off, so a caller reads which happened off the reply.
It is answered best-effort in `mlx_whisper/worker.py`, where the decline lives.

**This module is where that rule lives, once**, for exactly the reason
`diarize.speakers_or_raise` sits where it does. The endpoint refuses first,
before a job row opens and before a multi-gigabyte model downloads — the
runner is already resolved server-side (`registry.for_capability`), so the
answer is available immediately and making the user wait for a load to be
told "no" is a cost with no benefit. **Today the endpoint is the ONLY door**:
`parakeet-mlx` was the one runner that ever imported this module from its own
worker, and D406 withdrew it, so nothing on the worker side calls
`unsupported_or_raise` any more. That is a gap, not a design — the refusal
still has to happen in TWO places, because the bridge and the endpoint are
not the only doors into a worker process. **The next runner that adds an
`UNSUPPORTED` entry MUST also import this module in its own `worker.py` and
call `unsupported_or_raise` on arrival**, the way `parakeet_mlx/worker.py`
did, or an option refused at the endpoint can still be accepted-and-ignored
by whatever reaches the worker directly.

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
#: rows (`diffusers-image-rocm`, `llamacpp-text-vulkan`, …) need no entry and
#: have none: they answer exactly what their unaccelerated sibling answers,
#: which for both of those families is every option here, and the sibling rows
#: are absent too. But
#: the day a runner that DOES refuse something gains a CUDA or ROCm variant, the
#: variant needs its own entry — an unknown code refuses nothing (see
#: `unsupported_or_raise`), so the failure would be an accepted-and-ignored
#: option, which is the outcome this module exists to make impossible.
#:
#: **Empty for speech-to-text since D406** — both remaining runners answer
#: `task`, `language` and `initialPrompt` in full — **and no longer empty
#: overall**: the mflux-only base-image edit option (`fused.ai.image({image})`,
#: SPEC AI-9f) gave this table its first real rows since. The three diffusers
#: image codes each refuse `image` for the identical reason: the diffusers
#: pipeline's own image/edit signature is unverified on any machine this app
#: has run on (D428's "out of scope" note), and a
#: wrong guess there would be a broken engine that passes every test. All
#: three carry the SAME sentence because the fact is about the LIBRARY, not
#: about the wheel — `diffusers-image-cuda` and `diffusers-image-rocm` read
#: the identical pipeline class as the CPU row and would answer `image`
#: identically if it were ever wired up. Still one row per code, per this
#: table's own rule (a hardware variant that gains a refusal needs its own
#: entry) — three identical strings costs nothing and keeps the rule uniform
#: rather than special-cased for the one family that happens to agree with
#: itself today.
_DIFFUSERS_NO_EDIT = (
    "the Diffusers image engine renders from a prompt only — it has no "
    "base-image editing here. Switch this capability to the mflux engine "
    "on the AI Models page's Engines tab to edit an existing image, or drop "
    "'image' to render a fresh one."
)
UNSUPPORTED = {
    "diffusers-image": {"image": _DIFFUSERS_NO_EDIT},
    "diffusers-image-cuda": {"image": _DIFFUSERS_NO_EDIT},
    "diffusers-image-rocm": {"image": _DIFFUSERS_NO_EDIT},
}


def unsupported_or_raise(runner_code, *, task=None, language=None,
                         initial_prompt=None, image=None):
    """`ValueError` if `runner_code` cannot honour one of these, else None.

    Named arguments rather than the request dict, because the two callers hold
    the values in different shapes — the endpoint has a JSON body, the worker
    has the fields it already normalised — and a function that took the body
    would make the wire format part of this rule.

    An unknown or absent runner code refuses NOTHING, which is the honest
    default: this table is an exception list, and a code it has never heard of
    is an engine with nothing to say rather than an engine to distrust.

    `task` is refused on its VALUE and the other three on their PRESENCE. That
    asymmetry is the request's, not this module's: every transcribe request
    carries `task: "transcribe"` explicitly, while `language`, `initialPrompt`
    and `image` arrive as None (or, for `image`, simply absent) unless
    somebody asked for them.
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
    if image and "image" in rules:
        raise ValueError(rules["image"])
    return None
