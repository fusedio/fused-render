# Build-time notes on HANDOFF.md (thinking flag, D886)

This is a working log of places where the handoff needed clarification, a
scoping call I made that it did not spell out, or something worth flagging
for whoever reviews this build — kept separate from the repo's own
`DECISIONS.md`, which only gets the settled D886 row.

## Everything HANDOFF.md said about line numbers and file shapes checked out

No correction was needed to any of the six numbered "Files" sections —
`server/ai.py`'s `_TEXT_OPTIONS`/`_OPTION_NAMES`/warning-machinery shapes,
`mlx_text/worker.py`'s `_messages_to_prompt`/image-path locations,
`llama_text.py`'s `_render_chat`/`_prompt_text` locations, `runtime.js`'s
`textKeys`, and `fused_ai.py`'s `text()`/`stream()` signatures all matched
exactly what the handoff described, including the specific line numbers
given for a pre-compaction read. This is recorded here only because the
brief asked to log anything found wrong, and nothing was.

## Scoping call: no apple-tier warning test was written

`server/ai.py`'s apple-tier branch already had (pre-existing, before this
build) warnings for `effort` and `topP` that a local model does not gate at
all — and **neither of those has an existing test either**. There is no
`apple_host`-mocking fixture anywhere in `tests/test_ai_runtime.py` to hook
a new `thinking`-on-apple test onto; the Claude-tier equivalent
(`test_thinking_on_claude_is_a_warning_not_a_refusal`) works only because
`_claude_bin` is a simple, already-mocked seam. Building a new apple-host
fake from scratch to test one warning line, when the two warnings it sits
beside have never been tested that way, would be inventing test
infrastructure the rest of the apple branch doesn't have — not filling an
actual coverage gap this build introduced. Left it consistent with the
existing (untested) `effort`/`topP` apple warnings rather than making
`thinking` the one exception. The code path itself is verified by direct
reading (mirrors `effort`'s shape exactly, `_unsupported()` call included)
and by `ast.parse` on the whole file after editing.

## Correction: HANDOFF.md's "unset must pass no kwarg" was wrong

HANDOFF.md's `mlx_text/worker.py` section said unset `thinking` must pass NO
`enable_thinking` kwarg to `apply_chat_template` at all, so the templated
prompt stays byte-identical to before this flag existed, and the previous
build followed that instruction — `generate()` read `body.get("enable_thinking")`
straight through to `_messages_to_prompt`/`apply_chat_template` with no
defaulting of its own.

That is inconsistent with `llama_text.py`, which (also per this same
HANDOFF.md, section 3) forces `enable_thinking=True` into the Jinja render
context whenever the wire's `thinking` is unset (`generate()` at
`llama_text.py:1146`). The two runners therefore disagreed on what "unset"
means: `llama_text` always resolves it to thinking ON before rendering,
while `mlx_text` let the template's OWN default decide. For any model whose
chat template itself defaults thinking OFF, one `/api/ai` request with
`thinking` left unset would behave OPPOSITELY on the GGUF and MLX builds of
that same model — silently, with nothing to warn a caller. Both
`skills/fused-render-ai/SKILL.md` ("Local models default to thinking ON")
and `fused_render/templates/shared/fused_ai.py`'s `text()`/`stream()`
docstrings ("both local text runners default it ON") already promised a
contract only `llama_text` actually kept.

Fixed by moving the default into `mlx_text/worker.py`'s `generate()` —
`enable_thinking = body.get("enable_thinking"); if enable_thinking is None:
enable_thinking = True` — read in the same place `llama_text.py`'s
`generate()` reads it, so the two runners are symmetric side by side.
`_messages_to_prompt` keeps its own `None`-means-omit-the-kwarg contract
(tri-state, still directly usable and tested), but a real request from
`generate()` now always arrives with an explicit bool, so that branch is
exercised only by a direct caller who wants the template's raw default
(tests, or any future caller that isn't `generate()`).

One consequence worth flagging: this makes the `TypeError` retry (the
kwarg-rejected-by-a-tokenizer case documented in `_messages_to_prompt`'s
docstring) the ROUTINE path for any tokenizer that doesn't accept
`enable_thinking`, since a request essentially always carries an explicit
value now. That retry was silent (no exception, no log, no `warnings[]`
entry) before this fix — an explicit caller flag could be discarded with
nothing to diagnose it by, which is the exact undiagnosable S1-mini
degenerate-output case the whole flag exists to prevent. It now prints a
note to stderr naming the dropped flag, matching
`llama_text._prompt_text`'s existing discipline for a failed template
render.

## `test_thinking_on_claude_is_a_warning_not_a_refusal` was vacuous; an older test shares the defect

Code review finding 4. The test monkeypatched `_claude_bin` to `None`, so
`_ai_relay` always answered `ok: False` and the `if body.get("ok"):` branch
holding the `warnings[]` assertion was unreachable dead code — the test
could only ever assert that a bad-request error message doesn't mention
`'thinking'`, never that the warning actually appears.

Fixed by driving `_ai_relay` directly (not through `client.post`/starlette
TestClient) with `test_server_ai.py`'s `_cli_ok`/`_FakeProc` stubbing both
`_claude_bin` resolution and the CLI subprocess hop to a scripted success —
the same "avoid starlette TestClient" discipline that module's docstring
names, already reused this way by `test_ai_metrics.py`. Verified the fix is
not itself vacuous: temporarily removed the `thinking` warning append in
`server/ai.py`, confirmed the test fails (`assert [] == ['thinking']`), then
restored it.

**`test_sampling_on_claude_is_a_warning_not_a_refusal` (two tests up, same
file) has the identical defect** — also leaves `_claude_bin` unresolved, so
its `warnings[]` assertion is equally dead code today. Left unfixed per the
brief's scope (fix only the `thinking` test), but it should get the same
treatment.

## Order of file-touching, for anyone resuming

Wire surface first (`server/ai.py` + `runtime.js` + `test_ai_runtime.py`,
one commit), then the second client surface (`fused_ai.py` +
`test_fused_ai_client.py`), then each worker in its own commit
(`mlx_text/worker.py`, `llama_text.py`), then docs (`SPEC.md`,
`DECISIONS.md`, `skills/fused-render-ai/SKILL.md`) last, once the behaviour
they describe was already implemented and tested. This kept every commit
buildable and every test file green in isolation at each step.
