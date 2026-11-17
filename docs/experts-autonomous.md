# Autonomous experts: the contract

This replaces the approval-gated design in `docs/experts.md`. Where the two disagree, this file wins. `docs/experts.md`
is rewritten to match once the code lands.

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

Deleted: `proposals.py` (its section editor moves into `knowledge.py`, its drafting prompt into `distill.py`),
`cards.py`, `gate.py`, `shipped/*/questions.md`, the proposal/approval/card/checks/ask-the-user code paths everywhere,
and the lesson flags `proposed`, `promoted`, `rejected`.

## Files (under `<expert dir>/.fused/expert/` unless noted)

- `inbox` : today's observations store, unchanged fields (`id, ts, kind, claim, session, source, by_user, quote,
  after_advice, confirms, contradicts, outcome`). New kinds from chat: `taught` (claim), `forget` (lesson id or text).
- `memory.json` : lessons, today's shape minus `proposed`, `promoted`, `rejected`.
- `<expert dir>/skills/*.md` : one kind of skill; the expert may edit any of them.
- `skills-shipped/<name>.md` : the untouched version Fused shipped, written at seed time. Used for reset and updates.
- `activity.jsonl` : one line per thing the expert did (below).
- `history/<activity id>.json` : what `undo` needs for that line.
- `usage.jsonl` : the token ledger, unchanged.

## `knowledge.py` : the only writer

```python
apply(bot, change: dict) -> dict            # the activity line it wrote
undo(bot, activity_id: str) -> dict         # the activity line of the undo; raises knowledge.Refused
activity(bot, limit=100, before=None) -> list   # newest first
unread(bot) -> int ; seen(bot) -> None
```

`change` always carries `op`, `why` (one sentence, shown to the user) and optional `evidence`
(`{session, quote, source, item}`) and `stage`. Ops:

| op | fields | effect |
|---|---|---|
| `lesson_add` | `claim, kind, trust, via, session, about` | new lesson (or the existing equal one, upgraded) |
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

Activity line: `{id, ts, action, target, summary, why, evidence, stage, ok, undoable, undone, error}`.
`action` is what the user reads: `learned`, `counted`, `corrected`, `forgot`, `merged`, `removed`, `skill_updated`,
`skill_created`, `skill_restored`, `noticed`, `researched`, `could_not_find`, `skipped`, `undo`.
`counted` and `noticed` lines are `quiet: true`; the UI hides quiet lines unless asked.

Undo: a lesson change restores that lesson as it was (item level). A skill change restores the whole file as it was,
and is refused if the file no longer equals what that change left ("changed since; undo the later change first").
An undo is logged as its own line and marks the original `undone: true`.

## Learn : one loop

`consolidate.run(bot, force=False) -> dict` and `consolidate.status(bot) -> dict` keep their names.
Due when: three inbox entries wait, or the oldest is a day old, or any waiting entry is from the user
(`taught`, `forget`, or a correction with a verified quote), or `force`. Skipped entirely when the expert's manifest
has `expert.learning == false`.

A run is a list of independent steps. Each step takes the inbox and current knowledge and returns a list of changes;
the loop applies them through `knowledge.apply` in order. Steps do not call each other:

1. `lessons` : inbox entries into lesson changes (existing evidence rules, unchanged).
2. `research` : open unsure questions; found -> a lesson with its source; not found -> `note could_not_find`.
3. `distill` : lessons that have earned a skill (count >= `propose_at`, or a verified correction of a rule a skill
   states) -> one model call drafting section ops -> `skill_edit` or `skill_create`, then `lesson_remove` with
   `reason: "promoted"`. Applied at once.
4. `tidy` : the memory budget (limit 50, above 30), unchanged rules; promotion goes through `distill`.

The drafting prompt tells the model how to write skills (guidance only, nothing enforces it): prefer a reference to a
canvas file over copying it, write rules in the user's words, keep skills short, code only when it explains best.

Rules that carry over unchanged: a correction counts only with the user's quote found in the transcript; only
independent evidence raises `count`; a session lesson is used after two sessions; incremental capture; the read fence;
only a message typed at the computer may teach or forget; consult roster and fencing checks; 15 / 20 / 30 tool-call
caps.

## Chat

Tools: `remember_lesson` and `forget_lesson` write an inbox entry (`taught` / `forget`, `by_user` true only for a
message typed at the computer) and tell the wiring to run Learn now (`scheduler.poke(bot)`); `list_memory` stays.
`propose_skill` and `run_checks` are removed. Nothing is ever posted into the chat by the learning loop.

## HTTP (`/api/experts/{eid}/…`)

| Route | Returns |
|---|---|
| `GET activity?limit=&before=&quiet=` | `{items: [activity line…], unread, waiting, learning: {running, stage}}` |
| `POST activity/{aid}/undo` | `{ok, item}` or 409 `{detail}` |
| `POST activity/seen` | `{}` |
| `POST learn` `{force: true}` | `{started}` (replaces `consolidate`) |
| `GET memory` | `{items, budget}` (no flags, no archive) |
| `GET consults` | unchanged |
| `GET/POST settings` | adds `learning: bool` |
| `GET skills/health`, `POST skills/{name}/restore` | unchanged; restore goes through `knowledge.apply` |
| `GET /api/experts` | each expert adds `unread` |

Removed: `proposals`, `gaps`, `gaps/{gid}/dismiss`, `checks`, `checks/run`, `observations`, `consolidate`,
`memory/archive`, `memory/forget` (forgetting is taught in chat).

## UI

Tabs: **Activity** (default; the log with Undo on undoable lines, a "Learn now" button, a line "N things waiting"),
**Memory**, **Consults**. Removed: Observations tab, Checks tab, the skill approval card, "Recently removed".
An unread dot on the expert in the bots list when `unread > 0`; opening Activity calls `activity/seen`.
Settings (Expert tab): a "Learning" on/off switch.
