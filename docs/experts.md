# Experts: the contract

Code: `fused_render/experts/`. Tests: `tests/test_experts_*.py`. If code and this file disagree, raise it; do not
silently pick one. The first part is how an expert learns on its own; "Reference" below covers the bot kind, the
engines, the tools and the rules about evidence.

## Goal

The expert learns in the background with no approval step. It changes its own memory and skills silently and logs
everything it did. The code is three parts that never import each other and meet only at files.

## Parts and helpers

| Part | Modules | May write |
|---|---|---|
| Notice | `capture.py`, `observations.py` | the inbox |
| Learn | `consolidate.py` (the loop), `tidy.py`, `gaps.py` (research only), `distill.py` (lessons into skills) | nothing directly; only through `knowledge.apply` |
| Answer | `consult.py`, `chat.py` | the consult log; inbox entries (unsure, taught, forget) |

Helpers any part may import: `store`, `model`, `readtools`, `engine`, `usage`, `memory` (read functions),
`knowledge`. Wiring that may import parts: `routes.py`, `scheduler.py`, `seed.py`.

**Rule 1.** No module of one part imports a module of another part. A test enforces it.
**Rule 2.** `memory.json` and `skills/*.md` are written only inside `knowledge.py`. A test enforces it.
**Rule 3.** Nothing waits for a person. A change is applied and logged, or skipped and logged with the reason.

There are no proposals, approval cards, checks or questions to the user anywhere in this design.

## Files (under `<expert dir>/.fused/expert/` unless noted)

- `inbox` : the observations store (`observations/pending/*.json`, handled entries in `observations/done/`),
  fields (`id, ts, kind, claim, session, source, by_user, quote,
  after_advice, confirms, contradicts, need, tried, outcome`). `source` is `{target, backend}` plus, from capture,
  `canvas` (the canvas folder's name) and `files` (the paths that show a `worked` / `failed` entry). New kinds from chat: `taught` (claim), `forget` (lesson id or text).
- `memory.json` : the lessons. `memory-archive.jsonl` : lessons that left memory, with the reason.
- `<expert dir>/skills/*.md` : one kind of skill; the expert may edit any of them.
- `skills-shipped/<name>.md` : the untouched version Fused shipped, written at seed time. Used for reset and updates.
- `activity.jsonl` : one line per thing the expert did (below).
- `history/<activity id>.json` : what `undo` needs for that line.
- `usage.jsonl` : the token ledger. `consults.jsonl` : the consult log. `gaps.json` : Learn's open questions.
- `corrections.json` : Learn's skill corrections not yet written into their skill (below, step 3).
  `inbox-retries.json` : how often an inbox entry's change failed.

## `knowledge.py` : the only writer

```python
apply(bot, change: dict) -> dict            # the activity line it wrote
undo(bot, activity_id: str) -> dict         # the activity line of the undo; raises knowledge.Refused
activity(bot, limit=100, before=None) -> list   # newest first
unread(bot) -> int ; seen(bot) -> None
```

`change` always carries `op`, `why` (one sentence, shown to the user) and optional `evidence`
(`{session, quote, source, item}`; `source` is an object `{canvas, target, backend, files?}` when the entry came
from a task, and the file path when research found the lesson), `stage`, and `because` (the id of the activity line this change follows; see
Undo). Ops:

| op | fields | effect |
|---|---|---|
| `lesson_add` | `claim, kind, trust, via, session, about` | new lesson (or the existing equal one, upgraded); `kind` is the inbox kind, so `worked` and `failed` ("does not work") are lesson kinds |
| `lesson_bump` | `id, session` | count +1, once per session |
| `lesson_confirm` | `id, session` | `confirmed` +1; count unchanged |
| `lesson_contradict` | `id, session` | `contradicted` +1, once per session; count unchanged; logged quiet as `counted` |
| `lesson_edit` | `id, claim` | rewrites the claim; previous kept in the lesson's `history` |
| `lesson_remove` | `id, reason, into?, skill?` | lesson leaves memory |
| `lesson_merge` | `ids, claim` | one lesson replaces several; count = largest original |
| `skill_edit` | `skill, ops, triggers_add?, triggers_remove?` | section ops `add` / `replace` / `remove`; every unnamed section stays byte for byte |
| `skill_create` | `title, trigger, body` | new skill file |
| `skill_restore` | `skill` | back to the shipped version |
| `note` | `action, summary` | no state change; just a log line (`noticed`, `researched`, `could_not_find`, `skipped`) |

`apply` saves what undo needs, makes the change, reads a skill back to verify it (restoring on mismatch), and appends
the activity line. It never raises for a bad change: it logs `ok: false` with the reason and returns that line.
For a lesson change the snapshot is saved BEFORE the change is made, so a change that failed half way (the
lesson already written, a later step raised) is logged `ok: false` and still `undoable: true`.

Activity line: `{id, ts, action, target, summary, why, evidence, stage, ok, undoable, undone, error}`, plus
`because` when the change carried one.
`action` is what the user reads: `learned`, `noticed`, `counted`, `corrected`, `forgot`, `merged`, `removed`,
`skill_updated`, `skill_created`, `skill_restored`, `researched`, `could_not_find`, `skipped`, `undo`.

Honest labels for a lesson seen (`lesson_add`, `lesson_bump`); `knowledge.py` decides them, not the caller:

- `learned` : only when the change makes the lesson used in answers for the first time (`memory.indexed` turns
  true): a lesson added already in use (taught by the user, a verified correction, research), or the bump that
  takes a session lesson over the two-session threshold (`why`: "Seen in a second task. Now used in answers.").
- `noticed` : a first sighting that is not used in answers yet. `why` is the change's own sentence plus whether it
  is used: "You asked for this in a task. Not used in answers until a second task shows it." / "This worked in a
  task. Not used in answers until a second task shows it." / "This did not work in a task. I will look for what
  does."
- `counted` : every later sighting that changes nothing about use.

Quiet lines (`quiet: true`; the UI hides them unless asked): every `counted` line; a `noticed` line only when it
says so itself. `noticed` is quiet for a bare `repeated_request` and for an unsure question's note, and NOT quiet
for `worked`, `failed` and the other kinds. Read the line's `quiet` field, never the action name.

Undo: a lesson change restores that lesson as it was (item level). A skill change restores the whole file as it was,
and is refused if the file no longer equals what that change left ("changed since; undo the later change first").
An undo is logged as its own line (`why`: "You undid this.") and marks the original `undone: true`.

Linked undo: the `lesson_remove` that follows a skill change (reason `promoted`) carries `because: <the skill
line's id>`. Undoing the skill line also undoes every not yet undone line whose `because` is that id, each logged
as its own undo line (`why`: "Undone together with the skill change."), so the rule is never in neither place.
Undoing the lesson line alone stays allowed.

## Learn : one loop

`consolidate.run(bot, force=False) -> dict` and `consolidate.status(bot) -> dict` keep their names.
Due when: three inbox entries wait, or the oldest is a day old, or any waiting entry is a `failed` one (a failure
is researched at once), or any waiting entry is from the user
(`taught`, `forget`, or a correction with a verified quote), or `force`. Skipped entirely when the expert's manifest
has `expert.learning == false`.

A run is a list of independent steps. Each step takes the inbox and current knowledge and returns a list of changes;
the loop applies them through `knowledge.apply` in order. Steps do not call each other:

1. `lessons` : inbox entries into lesson changes (the evidence rules under "Capture"). `worked` -> a lesson of
   kind `worked`; `failed` -> a lesson of kind `failed` (a "does not work" lesson) AND an open question for step 2
   through `gaps.record` ("What works for: <need>? Tried and failed: <tried>."), under the gaps' own caps. Both are
   trust `outcome`: counted once per session, used in answers after two sessions, bumped only onto a lesson of
   their own kind (nothing else is ever counted toward a `failed` lesson).
2. `research` : open questions (an unsure consult, or what a `failed` entry left); found -> a lesson with its source; not found -> `note could_not_find`.
   At most `gaps.max_attempts` runs per question and one per day; at the cap, one more only when the skills or
   read roots changed. A question skipped because it is out of attempts gets ONE visible `note could_not_find`,
   once (`reported` on the gap), so it is never left as only a quiet "noticed" line.
3. `distill` : lessons that have earned a skill (count >= `propose_at`, or a verified correction of a rule a skill
   states) -> one model call drafting section ops -> `skill_edit` or `skill_create`, then `lesson_remove` with
   `reason: "promoted"` and `because: <the skill line's id>`. Applied at once. A correction of a skill rule is
   kept in `corrections.json`, one entry per (lesson, skill), until that skill's update succeeded; the lesson
   leaves memory only when no correction of it is left. A correction whose draft or update fails is tried again
   by the next run, at most 3 times, then dropped with one `note skipped` (the lesson stays in memory). A
   correction's lesson is never part of the ordinary draft of the same run. When the old rule's text is found
   in exactly one section of the skill, the draft must replace or remove THAT section.
4. `tidy` : the memory budget (limit 50, above 30; "Memory budget"); promotion goes through `distill`.

The drafting prompt tells the model how to write skills (guidance only, nothing enforces it): prefer a reference to a
canvas file over copying it, write rules in the user's words, keep skills short, code only when it explains best.

Rules detailed under "Reference": a correction counts only with the user's quote found in the transcript; only
independent evidence raises `count`; a session lesson is used after two sessions; incremental capture; the read fence;
only a message typed at the computer may teach or forget; consult roster and fencing checks; 15 / 20 / 30 tool-call
caps.

## Chat

Tools: `remember_lesson` and `forget_lesson` write an inbox entry (`taught` / `forget`, `by_user` true only for a
message typed at the computer) and tell the wiring to run Learn now (`scheduler.poke(bot)`); `list_memory` stays.
`forget_lesson` writes its entry when a stored lesson OR a `taught` entry still waiting in the inbox matches.
Nothing is ever posted into the chat by the learning loop.

## HTTP (`/api/experts/{eid}/…`)

| Route | Returns |
|---|---|
| `GET activity?limit=&before=&quiet=` | `{items: [activity line…], unread, waiting, learning: {running, stage}}` |
| `POST activity/{aid}/undo` | `{ok, item}` or 409 `{detail}` |
| `POST activity/seen` | `{}` |
| `POST learn` `{force: true}` | `{started}` |
| `GET memory` | `{items, budget}` |
| `GET consults` | `{consults}`, newest first |
| `GET/POST settings` | the manifest's settings, with `learning: bool` |
| `GET skills/health`, `POST skills/{name}/restore` | restore goes through `knowledge.apply` |
| `GET usage?days=`, `GET engines` | see "Usage ledger" and "Engines" |
| `POST capture` `{session_id}` | capture one session now |
| `GET /api/experts` | each expert, with `unread` |
| `POST /api/experts/consult` | see "The consult tool" |

POSTs require the `X-Fused` header. Forgetting is taught in chat; there is no route for it.

## UI

Tabs: **Activity** (default; the log with Undo on undoable lines, a "Learn now" button, a line "N things waiting"),
**Memory**, **Consults**.
An unread dot on the expert in the bots list when `unread > 0`; opening Activity calls `activity/seen`.
Settings (Expert tab): a "Learning" on/off switch.

---

# Reference

The sections below are the parts of the earlier design that still hold. Section numbers of that design
(`§4`, `§6`, …) that code comments still cite no longer exist; the headings below are what they refer to.

## The bot kind

A third bot kind, `expert`, beside `bot` and `super` (`fused_render/bots/bot.py` `KINDS`).

- It lives on the Bots page and has the normal chat thread.
- It has **no browser** and is **read-only**: it reads, lists and searches inside the folders its manifest names,
  plus its own `skills/`. Two exceptions: a consult can also read its caller's target folder ("The consult
  tool"), and on the Codex engine reads are not limited to those folders at all, only by Codex's own read-only
  sandbox ("Engines"). It never writes outside its own bot folder.
- Two ways in, sharing skills and memory. **Chat**: the user talks to it on the Bots page (one task at a time).
  **Consult**: a task session calls the MCP tool `expert_consult`; the server runs a separate, parallel, short
  run of the expert. Consults never touch the chat thread.

Integration with the bots code:

- `bots/bot.py`: `create()` accepts the kind (no greet model call: a fixed greeting); `start_task` runs
  `experts.chat.run` and skips the `headed()` check; `clone()` and the browser entry points (`wake_browser`,
  `takeover`, `popout`) refuse; `summary()` reports `browser: {"running": False}` and adds `expert: manifest`,
  `expert_available`, `expert_reason`; Super Bot's hand-off targets and BOTS section exclude experts.
- `bots/routes.py`: create accepts the kind; browser ops refuse for an expert with a sentence.
- `bots/store.py`: `"expert"` is in `PUBLIC_KEYS`. `bots/botfile.py`: an import keeps `kind == "expert"`.
- `bots/registry.py`: a seed step calls `experts.seed.seed()` where `seed_super` runs.
- An expert keeps its inert `Browser` object (plain paths); nothing may call `browser.start()`.

All JSON writes are atomic (temp file + `os.replace`). Every read-modify-write on one expert's files happens under
`experts.store.lock(bot)` (one `threading.RLock` per bot id). Machine state: `<home>/bots/experts-seeded.json`
(seed witness) and `<home>/bots/experts-captured.json` (capture watermarks); `<home>` is `bots.paths.root()`.

## The manifest (`bot.meta["expert"]`, public)

```json
{
  "ask_me_about": "Writing canvases, the canvas APIs, examples from existing canvases",
  "reads": [{"kind": "canvas-mirror", "scopes": ["team", "public", "user"]}],
  "offered_to": {"targets": ["canvas"]},
  "learns_from": ["worked", "failed", "repeated_request", "user_correction", "error_then_fix",
                  "answer_contradicted", "unanswered"],
  "engine": "auto",
  "model": "",
  "learning": true,
  "memory": {"consolidate_at": 3, "consolidate_after_s": 86400, "propose_at": 3, "index_lines": 200,
             "limit": 50, "tidy_at": 30, "stale_days": 14, "rare_uses": 2, "heavy_uses": 5},
  "gaps": {"research": true, "max_attempts": 2},
  "shipped": "canvas-reference"
}
```

- `experts.store.manifest(bot)` returns the manifest with every default filled in. Never read
  `bot.meta["expert"]` directly elsewhere.
- `reads` kinds: `canvas-mirror` (every environment root under the canvases root, the listed scope folders that
  exist) and `folder` (`{"kind": "folder", "path": "/abs"}`).
- `offered_to.targets`: `"canvas"` = sessions whose target is inside a canvas clone; `"*"` = every session.
- `learns_from`: the capture kinds plus `unanswered`. The legacy name `answer_contradicted` enables both
  `advice_confirmed` and `advice_contradicted`; either may also be named on its own. A manifest written before
  `worked` / `failed` existed still learns them: `repeated_request` also enables `worked`, `error_then_fix` also
  `failed`.
- `learning: false`: sessions are still noticed, the Learn loop never runs.
- `memory`: when a run is due (`consolidate_at`, `consolidate_after_s`), when a lesson has earned a skill
  (`propose_at`), the index cap, and the memory budget (below).
- `gaps`: `research: false` skips the research step (questions are still recorded); `max_attempts` = research
  runs per open question.

## Engines

Every model call an expert makes goes through `experts/model.py` (`text` for one answer, `tool_turn` for a run
with the read tools, an HTTP client of this server's own `/api/ai`). Nothing else may call a model; tests replace
those two functions.

| Engine | Picked by `auto` | Reads fenced by `readtools` | Token counts |
|---|---|---|---|
| `claude` | first, when enabled | yes | reported (input, output; no cache counts) |
| `copilot` | when Claude is off | yes | estimated |
| `codex` | never; only when the manifest names it | **no** (Codex keeps its own shell, `codex exec -s read-only`) | estimated |

- `provider_for(bot)` resolves `auto`; `available(bot)` is `(False, "needs Claude, Codex or Copilot")` when none
  is enabled; `fenced(bot)` is False on Codex. The settings page shows `ENGINE_NOTES["codex"]`: on Codex the
  expert can read files outside its folders, limited by Codex's own read-only sandbox, not by Fused.
- `model: ""` = the default: on Claude the bot's `model` meta when it is a Claude alias or a `claude-…` id,
  `"sonnet"` when that is a local model or another engine's; else the backend's default model.
- `GET /api/experts/{eid}/engines` lists the three with `enabled`, `models`, `default_model` and `note`.

One run of the expert is `engine.answer(bot, question, *, profile, …) -> {"answer", "refs", "used", "unsure"}`,
profile `chat`, `consult` or `research`. The system prompt is the role and rules, the bot's instructions, the
skills and `memory.index_text`. Skills on demand: the skills whose trigger words match the query go in full, at
most 2 (`FULL_SKILLS_MAX`); every other skill is a SKILL INDEX entry (name, title, trigger line, its `## `
headings) the expert opens with `read_file` when it needs it. Tool calls per run are capped (consult 15, chat 20,
research 30); past the cap every call returns an error telling the model to answer now. The model finishes with
a fenced JSON block; `parse` is lenient (the last block that parses wins; no block: the whole text is the answer)
and the reader never sees the block.

## The read-tool fence (`experts/readtools.py`)

The only way an expert touches the disk. Tools: `read_file {path, page?}` (text, 20k characters per page),
`list_dir {path?}` (no path = the roots), `search {query, path?, content?, glob?}`. Roots are
`resolve_reads(manifest)` + the expert's skills folder + a consult's extra roots.

- A path is allowed only when its real path is inside a root. Anything else returns `("not allowed: …", True)`.
  The handler never raises.
- A root that is `/`, the home folder or an ancestor of it is refused (`too_broad`).
- Below its root, a path with a dot-prefixed component (`.env`, `.git/config`, `.ssh/…`) or a name on the secret
  denylist (`*.pem`, `*.key`, `id_rsa*`, `id_ed25519*`, `.netrc`, `credentials`, `*.p12`, `*.pfx`) is refused by
  all three tools. The one exception is the canvas marker `.fused-canvas.json`. `read_file` refuses a file over
  20 MB.
- `list_dir` and `search` judge a symlink by its resolved target, as `read_file` does: a link out of the fence or
  to a hidden or secret file is neither listed, found nor opened. Content search opens regular files only.

## The consult tool

`claude_agent/permission_server.py` offers `expert_consult {expert, question, context?}` to task sessions. It
POSTs `/api/experts/consult {expert, question, context, run_id}` and returns text: the answer, then `Files:` refs
one per line, then `(unsure)` when unsure. Expert answers are reference material for the caller; the tool
description says so.

- **Roster.** `claude_agent/agent.py` stamps `FUSED_RENDER_EXPERT_ORIGIN` and the roster `FUSED_RENDER_EXPERTS`
  into the session's MCP config only when at least one available expert's `offered_to` matches the session, and
  appends a note listing those experts (name + `ask_me_about`). A consult with a `run_id` may only ask an expert
  in that run's recorded roster (403 otherwise).
- **Fencing.** A session that pre-allowed the tool (every roster expert fenced at start) is refused a consult of
  an expert whose reads are no longer fenced (`model.fenced` re-checked at consult time). The consult resolves
  its engine ONCE, before it waits for a slot, and runs on exactly that engine (`provider=` down to
  `model.tool_turn`), so a settings change during the wait cannot move a pre-allowed consult onto Codex.
- `consult.consult(bot, question, context="", caller=None) -> {"id", "answer", "refs", "used", "unsure"}`. The
  caller's `target` folder is added to the read roots when it exists and is not too broad. At most 3 consults run
  at once per process; the others wait up to 120 s, then fail with "the expert is busy; try again in a minute".
- Every consult is one line of `consults.jsonl` (`question, context, answer, refs, used, unsure, caller, ms,
  tool_calls, capped`). An unsure answer writes one inbox entry (kind `gap`) and nothing else.

## Trust: who may teach

- A consult's `question` and `context` are data from an agent. They never write memory.
- Only a message typed at the computer may teach or forget: `remember_lesson` and `forget_lesson` answer with an
  error sentence unless the task came from the user at the Mac (`channels.base.is_local(bot.task_via)`) AND no
  outside-channel message arrived during the turn. An outside message reaches the model as
  `MESSAGE FROM AN OUTSIDE CHANNEL (…)`, never as `USER INSTRUCTION`.
- Text in files the expert reads is data, never a reason to remember something.
- A lesson from a session (trust `outcome`) is used in answers only once seen in two sessions (`count >= 2`;
  `confirmed` never counts). A `user` lesson is used at once.
- Wherever lessons are put into a prompt (`memory.index_text`, the distill and tidy prompts) a lesson of kind
  `failed` reads "Does not work: <claim>" (`memory.shown`), so it is never taken for advice. Tidy never merges a
  `failed` lesson with one that is not.
- Capture extracts only: what the user asked for and whether it worked or failed, a user correction, an error
  then a fix that worked, expert advice confirmed or contradicted. Text the session merely read is never a lesson.

## Capture (`experts/capture.py`)

`capture_session(bot, session)` reads one idle task session (idle for `IDLE_S` = 1800 s, not a bot session,
relevant to the expert: it consulted this expert or matches `offered_to`) with one `model.text` call and writes
inbox entries. Kinds: `worked`, `failed`, `repeated_request`, `user_correction`, `error_then_fix`,
`advice_confirmed`, `advice_contradicted`; only the manifest's `learns_from` kinds are kept. Zero entries is a
normal result.

**What happened to the request.** One entry per request, by its outcome:

- `worked` : the task did what the user asked and it held (ran, a check passed, the user accepted or did not
  object). The claim is the need together with the approach that satisfied it, written as a reference (which
  file, dataset or call), never copied code; `source.files` names the files in the canvas that show it.
  `after_advice` when the approach is only what this expert advised in that session.
- `failed` : the task could not do what was asked, or an approach was tried, did not work, and no fix was found
  in the session (an error that WAS fixed stays `error_then_fix`). The claim is the need, what was tried and why
  it failed; `need` and `tried` carry the two parts for the research question. It may carry `contradicts` when
  the failed approach was the expert's own advice (handled like `advice_contradicted` by outcome: the old lesson
  is marked contradicted).
- `repeated_request` : only for a request whose outcome is unknown. The prompt asks for ONE `worked` or `failed`
  entry instead whenever the outcome is known.

**Prompt hygiene.** The consult records and the transcript sit between `<<<BEGIN …>>>` / `<<<END …>>>` markers
the prompt declares DATA. Every transcript line carries its speaker (`USER| `, `AGENT| `, `TOOL| `, `NOTE| `),
and a line inside agent or tool text that starts like a speaker tag is prefixed `(quoted) `, so nothing but a
real user turn reads as the user.

**Grounding.** For `repeated_request`, `user_correction` and an `advice_contradicted` that claims the user said
so, the model must return `quote`: a verbatim excerpt (at least 12 characters) of a USER line. `capture.grounded`
checks it is a substring of one of the user turns the model was shown. Verified: `by_user: true` and the quote is
kept. Not verified: `by_user: false`, no quote, ordinary session evidence. The model's own `by` is never trusted
alone. `after_advice`: the user said it only after, and in line with, this expert's advice in that session.

**Evidence.**

- `confirms` / `contradicts` are kept only when they name an existing lesson (`contradicts` may also name a
  lesson already written into a skill). A `confirms` is credited with no model decision.
- **A verified user correction takes effect at once**: its lesson is stored with trust `user` and
  `via: "session"`. When it contradicts a lesson in memory, one model call per pair decides `replace` (the old
  lesson leaves with reason `corrected`) or `narrow` (`lesson_edit`; the old wording stays in `history`). No
  readable decision: the old lesson is only marked contradicted. When the old rule lives in a skill, distill
  rewrites that skill's section (a draft that only adds a rule is refused).
- **Only independent evidence raises `count`**, once per session: a grounded `repeated_request` or
  `user_correction` that is not `after_advice`, an `error_then_fix`, a `worked` that is not `after_advice`, a
  `failed`, and teaching in chat. Anything else, chiefly
  `advice_confirmed` and `after_advice`, is a `lesson_confirm`; a new lesson from it starts at count 0.
- A failed or unreadable model answer in the lessons step adds nothing: the entries stay waiting and the
  failure is counted (`consolidate-failures.json`, one `note skipped` line per try). After 3 consecutive failed
  or unreadable answers (`FALLBACK_AFTER`) the waiting entries are added WITHOUT the model's filtering decision:
  each claim becomes a lesson, or is counted toward the lesson with exactly the same text (the evidence rules
  above still apply), so the queue cannot grow forever. A usable answer resets the count.
- An entry whose change `knowledge.apply` could not make (`ok: false`) is not archived: it stays waiting and is
  tried again by the next run, at most 3 times (`inbox-retries.json`); then it is archived with outcome
  `failed` and one `note skipped` line says so.
- A `forget` entry also cancels every older `taught` entry still waiting in the inbox whose text it matches:
  both are archived and the lesson is never added.

**Only what is new.** Per (session, expert) the watermark keeps how many turns were read and which consult ids
were seen. A later capture sends only the turns after that point (preceded by at most 2 earlier turns marked as
already processed). When the new part holds no USER turn and no new consult, the session is marked read with no
model call. Lessons the session already produced are listed as not to be reported again, and an equal claim is
dropped by code. A capture that raises 3 times at one transcript size is given up for that size, so it cannot
block the queue.

## Memory budget (`experts/tidy.py`)

Memory holds at most `limit` (50) lessons; once it holds more than `tidy_at` (30), every Learn run ends by
tidying it. Tidy writes nothing itself: it yields `lesson_remove` / `lesson_merge` changes.

- **Usage.** `count` = sessions that showed the lesson; `cited` = consult or chat answers whose `used` list named
  it; `uses = count + cited`. Idle time runs from the later of `updated` and `last_used`.
- **Code first.** With no model, code finds the delete candidates (`uses <= rare_uses` (2), idle for
  `stale_days` (14) or more, not taught by the user) and the merge candidates (groups of 2 or 3 lessons sharing
  an `about` tag or enough claim words; contradicted and heavily used lessons are left out; at most 10 groups).
- **One model call**, only when there are candidates, sees only those lessons and says which to delete and which
  groups to merge into what claim. Code still decides: a delete must be a delete candidate; a merge must name 2
  or 3 lessons of ONE shown group with a claim of 1 to 400 characters; at most 5 merges per tidy; each lesson
  belongs to the first action naming it. A failed call or an unreadable answer deletes and merges nothing.
- **A merged lesson** takes `count` = the LARGEST original count (two lessons each seen once are not a lesson
  seen twice), the summed `cited`, the union of sessions and `about`, and trust `user` only when every original
  was the user's.
- **Heavily used lessons** (`uses >= heavy_uses` (5) AND `count >= 2`, not contradicted) are not tidied away:
  `distill` writes them into a skill. Being cited alone never promotes a lesson.
- **Skipped when nothing changed.** While the memory fingerprint (ids, counts, cited, contradicted, the day
  number, the budget) equals the one saved by the last tidy that changed nothing, the review makes no model call.
- **The hard limit**, with no model: while memory holds more than `limit`, lessons leave as `over_limit`, first
  those not taught by the user (fewest uses, then longest idle); a user-taught lesson only when nothing else is
  left.

Every lesson that leaves memory is appended to `memory-archive.jsonl` with `removed_at` and its `reason`:
`promoted` (a skill carries it now), `forgotten`, `corrected`, `stale`, `merged`, `over_limit`. Archived lessons
are out of every prompt.

## Usage ledger (`experts/usage.py`)

Every model call writes one line of `<expert dir>/.fused/expert/usage.jsonl`, success or failure:
`{ts, stage, provider, model, input_tokens, output_tokens, cache_read_tokens, rounds, ms, ok, estimated}`.
Accounting never fails the call.

- Stages: `consult`, `chat`, `research`, `capture`, `consolidate`, `tidy`, `draft` (distill), `other`. The engine
  labels its own runs by profile; otherwise the stage is the calling module's.
- The Claude tier reports input and output tokens (no cache counts, so `cache_read_tokens` is null). Copilot and
  Codex report nothing: the line is an estimate (`estimated: true`, characters / 4, a tools turn re-sending the
  prompt and the tool traffic every round).
- `GET /api/experts/{eid}/usage?days=7` returns `usage.summary`: `{days, by_stage, by_day, total}`.

## Developer settings

For testing the automatic loop end to end in a dev environment only; never set them in production. Unset,
malformed or non-positive values leave the defaults in place; each is read when used, not at import.

- `FUSED_RENDER_EXPERT_IDLE_S`: idle time before a task session is captured (default 1800).
- `FUSED_RENDER_EXPERT_TICK_S`: scheduler tick interval (default 300, floored at 5).
- `FUSED_RENDER_EXPERT_START_DELAY_S`: delay before the first tick (default 90, floored at 5).
