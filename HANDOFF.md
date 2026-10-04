# Handoff: a per-request `thinking` flag for local text generation

## Why

`mlx-community/S1-mini-MLX-4bit` (a Qwen3-0.6B transcript normalizer, used by the
OpenWhisper app) is unusable through `fused.ai.text()`. Its model card is explicit
that `enable_thinking=False` is **required** — "If you leave the flag out you will
usually get no usable output at all."

`mlx_text/worker.py::_messages_to_prompt` never passes `enable_thinking`, so the
prompt ends `<|im_start|>assistant\n` instead of the
`<|im_start|>assistant\n<think>\n\n</think>\n\n` the model was trained on, and
there is no way for a caller to ask for anything else.

Reproduced outside fused-render entirely, in the mlx-text runner's own venv
(`~/.fused-render/venvs/449c06df81a542d1`), same model, same system prompt, input
`"so um i need to like send the the report by uh friday no wait make that thursday"`:

| prompt tail | temp | output |
|---|---|---|
| `…assistant\n` (today) | 0.7 | `assistant:\nA. Thursday 2. B. Thursday 3…` (degenerate loop) |
| `…assistant\n` (today) | 0.0 | `assistant:\nassistant:\nHello there. So I think…` |
| `…assistant\n<think>\n\n</think>\n\n` | 0.7 | `So I need to send the report by Thursday.` |
| `…assistant\n<think>\n\n</think>\n\n` | 0.0 | `So I need to send the report by Thursday.` |

**The chat template itself is fine.** `chat_template.jinja` is fetched
(`download_snapshot(model_id)` is unscoped) and transformers 5.15 loads it into
`processor.chat_template`; the rendered prompt carries correct ChatML markers. An
earlier theory that `.jinja`-packaged templates go unapplied was checked and is
**wrong** — do not spend time on it.

## The two runners disagree today

| runner | text path | reachable by caller |
|---|---|---|
| `mlx_text/worker.py` | passes nothing → template default → thinking **ON** | no |
| `llama_text.py::_render_chat` | `enable_thinking=False` **unconditionally** (:1013) | no |

SPEC **AI-11d** states "Reasoning is OFF by default" — true of `llama_text.py`,
false of `mlx_text`. A side effect: the **GGUF** build of S1-mini works today by
accident while the MLX build is broken.

## What to build

A per-request flag, threaded client → wire → worker → template, **defaulting to
thinking ON in both local text runners when unset**.

This reverses AI-11d. It is the owner's explicit decision (2026-09-21), taken with
the costs named: S1-mini's GGUF build — which works today — will need
`thinking: false` after this lands, and slow CPU machines get think tokens by
default. Do not re-litigate it; record it.

Wire/client name is **`thinking`** (boolean). The worker's vocabulary stays
`enable_thinking` — `server/ai.py` is the one place camelCase meets snake_case
(D633), exactly as it already does for `maxTokens`/`topP`.

Tri-state: unset / `true` / `false`. Unset must be distinguishable from `false`.

## Files, with what is already verified about each

### 1. `fused_render/server/ai.py`

- `_TEXT_OPTIONS` (:806) — add `"thinking"`. The envelope is closed (D413, extended
  to text by D633): an unrecognised key is a 400 naming it.
- Worker request build (:1063-1070, beside `max_tokens`/`temperature`/`top_p`) —
  add `"enable_thinking": body.get("thinking")`.
- **Validate the type.** A non-boolean `thinking` is a 400, like the other typed
  fields. Follow whatever `temperature`/`topP` already do for bad values.
- **Non-local tiers get a warning, not a 400.** D631 is the rule: a *tunable* a
  tier lacks produces `{type: "unsupported-setting", setting, message}` in
  `warnings[]`; only *semantic* flags (`history`, `raw`, `images`) keep the 400.
  `thinking` is a tunable. Mirror exactly how `effort` is handled when it reaches a
  local model — `_OPTION_NAMES` (:815) and the warning machinery around it are the
  precedent. Apple and Claude tiers warn.

### 2. `fused_render/ai/runners/mlx_text/worker.py`

- `_messages_to_prompt` (:402-435) — take the flag. **Unset must pass no kwarg at
  all**, so the default prompt stays byte-identical to today's; an explicit
  `True`/`False` passes `enable_thinking=<bool>`.
- **Retry hazard, do not skip.** transformers' `apply_chat_template` can raise
  `TypeError` on a tokenizer/template that does not accept the keyword. AI-11d
  documents this exact retry for the removed transformers runner: catch it, retry
  without the kwarg, and let the call succeed. `llama_text.py`'s module docstring
  (:158-172) explains why Jinja needs no such retry and transformers does — read it.
- `generate` (:612) — thread `body.get("enable_thinking")` in.
- Image path (:607-610) already passes `enable_thinking=True` unconditionally to
  `mlx_vlm.prompt_utils.apply_chat_template`. Honour an explicit flag there too,
  still defaulting to `True` when unset.
- The docstring at :402-429 currently argues thinking must always stay on and is
  load-bearing for the mlx-lm→mlx-vlm switch (#806). It has to be rewritten to say
  what is now true: the default is still on, and the caller can now say otherwise.
  Keep the part explaining why `mlx_vlm.prompt_utils.apply_chat_template` must not
  be reached for on the text path — that reasoning is unchanged and still correct.

### 3. `fused_render/ai/runners/llama_text.py`

- `_render_chat` (:990-1013) — take the flag; **default flips `False` → `True`**.
  Jinja ignores an unreferenced context variable, so no retry is needed here.
- `_prompt_text` (:1034+) and its caller thread `enable_thinking` from the request
  body through.
- This file backs both `llamacpp_text` and `llamacpp_text_vulkan` (five-line shells
  around it). One change, both engines.
- The module docstring (:158-172) states the unconditional `False` and its
  rationale. Update it.

### 4. `fused_render/static/runtime.js`

- `textKeys` (:3529) — add `"thinking"`. `rejectUnknownOptions` (:3531) is what
  turns an unknown option into a 400 in the bridge.

### 5. `fused_render/templates/shared/fused_ai.py`

- `text()` (:471) and `stream()` (:499) both gain `thinking: bool | None = None`,
  set on the body only when not `None`. D470 requires this module mirror
  `runtime.js`'s surface 1:1, names and option names.
- Stdlib-only, no `fused_render` import — that constraint is absolute here.

### 6. Docs

- **SPEC.md AI-11d** (:8681-8703) — rewrite. It currently asserts reasoning is off
  by default, which becomes false. State the new default (on, both runners), the
  `thinking` flag, and that the GGUF path's default moved.
- **SPEC.md** llamacpp section (~:8811) — repeats "`enable_thinking=False` rides
  into the render context unconditionally". Fix it.
- **DECISIONS.md** — add a row for this decision: the per-request flag, the
  unify-ON choice, the costs accepted (S1-mini GGUF needs the flag; CPU think
  tokens), and the rejected alternatives (keep per-runner status quo; unify OFF per
  AI-11d). Use the next free D number. **Note:** CI tests the merge ref, so a
  D-number taken by a branch that lands first will collide — if CI reports a
  duplicate-decision-id failure, renumber, it is not a logic error.
- **`skills/fused-render-ai/SKILL.md`** — this is the skill a page author reads
  before calling `fused.ai`. Document `thinking`, and say plainly that a model
  whose card demands thinking off (S1-mini is the live example) needs
  `thinking: false`.

## Tests

Scoped runs only — the full suite is the orchestrator's job, not yours.

- `tests/test_ai_mlx_worker.py` — the text path currently asserts only *which*
  template helper is called (:546, :706). Add: unset passes no kwarg; `true`/`false`
  pass through; the `TypeError` retry drops the kwarg and still returns a prompt.
  The existing fakes (`_ProcessorWrappingATokenizer`, `_fake_mlx_vlm_with_config`)
  are where to hook this.
- `tests/test_ai_llamacpp_worker.py` — the render context gets the flag; the
  **default is now `True`**. There is an existing `"{{ enable_thinking }}"` template
  fixture at :575 to build on.
- `tests/test_ai_runtime.py` — `thinking` is accepted, a non-boolean is a 400, an
  unknown key still 400s, and the value reaches the worker request as
  `enable_thinking`.
- **Add the missing drift guard.** `test_ai_runtime.py` has source-text drift tests
  pinning `imageKeys` (:9194) and `transcribeKeys` (:9336) against the Python
  whitelists — there is **none for text**, which is exactly the drift this change
  could introduce. Add one for `textKeys` vs `_TEXT_OPTIONS`, in the same shape.
- A test asserting the non-local tiers warn rather than 400 on `thinking`.

## Out of scope

- Temperature. S1-mini's card says decode greedily, and the server defaults to 0.7
  — but with thinking off it returned the correct answer at 0.7 in the repro above,
  so this is not needed for the fix and is not part of it.
- Any per-model catalog metadata that would set the flag automatically. That was
  the alternative design (option 1) and was not chosen.
- The OpenWhisper app itself. It lives outside this repo.

## Working notes

- Run `setting-up-dev-env` (repo skill) **first**. A fresh worktree silently tests
  the main checkout otherwise.
- Do not start a dev server; the user runs `dev.sh` themselves.
- Commit per logical unit, clear messages. Do not squash.
- Append anything you learn that this handoff got wrong to `DECISIONS-LOG.md` in
  this worktree (not `DECISIONS.md`, which is the repo's own).
