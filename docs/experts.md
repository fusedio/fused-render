# Experts — architecture and contracts

Status: build spec, 2026-11-16. Design page: <https://claude.ai/artifact/Xh8W3zP9D1MLPKoZKvtBWY>.
This file is the contract every implementer works from. If code and this file disagree, raise it; do not
silently pick one.

## 1. What an expert is

A third bot kind, `expert`, beside `bot` and `super` (`fused_render/bots/bot.py` `KINDS`).

- It lives on the Bots page and has the normal chat thread.
- It has **no browser** and is **read-only**: it can read, list and search inside the folders its manifest
  names, plus its own `skills/`. It never writes outside its own bot folder.
- It has two ways in, sharing skills and memory:
  - **Chat**: the user talks to it on the Bots page (one task at a time, the bot's own thread).
  - **Consult**: an agent session (a Task) calls the MCP tool `expert_consult`; the server runs a separate,
    parallel, short run of the expert and returns a structured answer. Consults never touch the chat thread.
- It learns: observations are captured from chat teaching, unsure consults and idle task sessions; they are
  consolidated into memory; lessons that keep recurring are proposed as skill changes on an approval card.
- Engines: `claude`, `codex`, `copilot` only (`agent_backends.registry`). Selected per expert
  (`auto` follows the Preferences default when it is one of the three, else the first enabled of the three).

Out of scope now: expiry, item stages, per-item git history, merging shipped updates, sharing between people.

## 2. Where things live

Python package `fused_render/experts/` (new). Tests `tests/test_experts_*.py`.

Bot folder (bots v3 layout, `bots/paths.py`):

```
<workspace>/bots/<folder>/
  bot.json                      public; gains key "expert" (the manifest, section 3)
  skills/<name>.md              the bot's skills (existing format: "# Title\ntrigger: a, b\n\n<body>")
  questions.md                  the quality gate's fixed questions (section 9)
  .fused/expert/
    memory.json                 {"version":1,"items":[Item]}
    observations/pending/<id>.json
    observations/done/<id>.json
    consults.jsonl              one Consult record per line, newest last
    checks/<run_id>.json        gate runs; checks/latest.json is a copy of the newest finished run
    proposals/<id>.json         skill proposals
```

Machine state: `<home>/bots/experts-seeded.json` (seed witness), `<home>/bots/experts-captured.json`
(session capture watermarks). `<home>` is `bots.paths.root()`.

All JSON writes are atomic (temp file + `os.replace`). All read-modify-write on one expert's files happens
under `experts.store.lock(bot)` (one `threading.RLock` per bot id).

## 3. The manifest (`bot.meta["expert"]`, public, add `"expert"` to `store.PUBLIC_KEYS`)

```json
{
  "ask_me_about": "Writing canvases, the canvas APIs, examples from existing canvases",
  "reads": [{"kind": "canvas-mirror", "scopes": ["team", "public", "user"]}],
  "offered_to": {"targets": ["canvas"]},
  "learns_from": ["repeated_request", "user_correction", "error_then_fix", "answer_contradicted", "unanswered"],
  "engine": "auto",
  "model": "",
  "memory": {"consolidate_at": 3, "consolidate_after_s": 86400, "propose_at": 3, "index_lines": 200},
  "shipped": "canvas-reference"
}
```

- `reads` kinds: `canvas-mirror` (every environment root under the canvases root, the listed scope folders
  that exist) and `folder` (`{"kind":"folder","path":"/abs"}`).
- `offered_to.targets`: `"canvas"` = sessions whose target is inside a canvas clone; `"*"` = every session.
- `engine`: `auto | claude | codex | copilot`. `model`: `""` = default (claude: the bot's `model` meta,
  else the backend's default model).
- `experts.store.manifest(bot)` returns the manifest with every default filled in. Never read
  `bot.meta["expert"]` directly elsewhere.

## 4. Module map and the public functions each module owns

Every function below is a contract. Names, parameters and return shapes are fixed. `bot` is a
`fused_render.bots.bot.Bot`.

### `experts/store.py`
```
is_expert(meta_or_bot) -> bool
manifest(bot) -> dict                      # defaults filled
expert_dir(bot) -> str                     # <bot.dir>/expert, created
lock(bot) -> threading.RLock
read_json(path, default) ; write_json(path, obj)          # atomic
append_jsonl(path, obj) ; read_jsonl(path, limit=None) -> list
all_experts() -> list[Bot]                 # every bot of kind expert (bots registry)
find(id_or_name) -> Bot | None             # id, else exact name (case-insensitive), else manifest "shipped" key
new_id(prefix) -> str                      # e.g. "m3f9a1c2"
```

### `experts/model.py` — every model call an expert makes
```
ENGINES = ("claude", "codex", "copilot")
provider_for(bot) -> tuple[str, str]       # (provider, model) after resolving "auto"; raises Unavailable
available(bot) -> tuple[bool, str]         # (False, "needs Claude, Codex or Copilot") when none is enabled
text(bot, prompt, *, system="", timeout=180) -> str
tool_turn(bot, prompt, *, system, tools, handler, timeout=600) -> str
class Unavailable(RuntimeError)
```
- `text` goes through `bot._ai_call(botmod._fused_ai(), prompt, provider=..., model=..., system_prompt=...)`
  (the usage ledger sees it).
- `tool_turn` is an HTTP client of this server's own `/api/ai` (`stream: true`, `tools`, `provider`,
  `model`, `systemPrompt`): it reads the NDJSON stream, and for each `{"type":"tool_call", id, name, input}`
  frame calls `handler(name, input) -> (text, is_error)` then POSTs
  `/api/ai/turns/<turn>/results {id, content, isError}`. It returns the final text. Origin from
  `FUSED_RENDER_ORIGIN`; header `X-Fused: 1`. `server/ai.py` is NOT modified.
- `tools` is the gateway shape: `[{"name", "description", "parameters": <JSON Schema>}]`.
- Tests replace `model.text` / `model.tool_turn` with fakes (monkeypatch). Nothing else may call a model.

### `experts/readtools.py` — the only way an expert touches the disk
```
resolve_reads(manifest) -> list[str]               # absolute existing folders
specs() -> list[dict]                              # tools: read_file, list_dir, search
make_handler(roots, on_tool=None) -> handler       # handler(name, input) -> (text, is_error)
```
- `read_file {path, page?}` (text, 20k chars per page), `list_dir {path?}` (no path = the roots),
  `search {query, path?, content?: bool, glob?}` (file names, or with `content` matching lines; capped).
- The fence: a path is allowed only when `bots.bot.inside(realpath, roots)` is not None. Anything else
  returns `(“not allowed: …”, True)`. Never raises.
- `on_tool(name, input, result_text)` is called after each call (chat uses it to emit `action` events).

### `experts/memory.py`
```
Item = {"id","claim","kind","trust","count","sessions":[...],"about":[...],"created","updated",
        "proposed": bool, "promoted": bool, "rejected": bool}
items(bot) -> list[Item]
add(bot, claim, *, kind="lesson", trust="outcome", session="", about=None) -> Item
        # a claim equal after normalisation (lowercase, collapsed whitespace, no trailing punctuation) bumps instead
bump(bot, item_id, *, session="") -> Item | None      # count += 1 only when `session` is new or empty
forget(bot, id_or_text) -> int                        # removed count
mark(bot, item_id, **flags) -> Item | None
index_text(bot) -> str     # "- [<id>] <claim> (seen N×)", most seen first, capped at manifest index_lines
```
`trust`: `user` (said directly in chat), `outcome` (from a session), `gap` (an unanswered consult).

### `experts/observations.py`
```
Obs = {"id","ts","kind","claim","trust","session","about":[...],"consult": ""}
write(bot, *, kind, claim, trust, session="", about=None, consult="") -> Obs
pending(bot) -> list[Obs]                  # oldest first
archive(bot, ids) -> None                  # pending/ -> done/
due(bot, now=None) -> bool                 # len(pending) >= consolidate_at, or oldest older than consolidate_after_s
```
`kind` is one of the manifest's `learns_from` values, or `"gap"`.

### `experts/consolidate.py`
```
run(bot, *, force=False) -> {"ran": bool, "added": [ids], "bumped": [ids], "ignored": int}
```
Not due and not `force` → `{"ran": False, ...}`. One `model.text` call: current items + pending
observations → JSON list of `{"obs": id, "op": "add" | "bump" | "nothing", "item": id?, "claim": str?}`.
Code applies the edits (`memory.add` / `memory.bump`), archives the observations, returns. If the model's
answer cannot be parsed, fall back to `memory.add` per observation (exact-match dedupe) so nothing is lost.
Observations with trust `gap` are never added as lessons; they are counted in `ignored` (they stay visible
in `done/`).

### `experts/gate.py` — the quality gate
```
questions(bot) -> list[{"q": str, "must": [str]}]      # parsed from <folder>/questions.md, section 9
run(bot, *, skills_override=None, answer_fn=None) -> Run
Run = {"id","ts","score": int,"total": int,"results":[{"q","passed": bool,"why": str,"answer": str}],
       "engine": str}
start(bot) -> str            # run in a background thread; returns run id; one at a time per bot (else returns the running id)
status(bot) -> {"running": bool, "run_id": str|None, "done": int, "total": int}
latest(bot) -> Run | None
```
`answer_fn(bot, question, skills_override) -> str` defaults to `experts.engine.answer_question`. Each answer
is graded by one `model.text` call that must return JSON `{"passed": bool, "why": str}` against the `must`
points. Up to 3 questions run in parallel. `run` persists the finished run unless `skills_override` is set.

### `experts/proposals.py`
```
Proposal = {"id","ts","status": "ready"|"shown"|"approved"|"rejected","action": "update"|"create",
            "skill": name|None,"title","trigger","body","diff": str,"items":[item ids],
            "score_before": {"score","total"}|None,"score_after": {"score","total"}|None,"card_seq": int|None}
candidates(bot) -> list[Item]       # count >= propose_at and not proposed/promoted/rejected, trust != gap
prepare(bot, items) -> Proposal     # draft (one model.text call: new or updated skill) + gate before/after; saved as "ready"; items marked proposed
get(bot, pid) -> Proposal | None ; listing(bot) -> list[Proposal]
approve(bot, pid) -> Proposal       # bot.skill_save(...), items marked promoted
reject(bot, pid) -> Proposal        # items marked rejected
card_text(proposal) -> tuple[str, str]   # (text, detail) for the approval event; detail is the unified diff
```
Updating an existing skill is preferred to creating one. The gate scores are skipped (None) when the expert
has no questions.

### `experts/engine.py` — one run of the expert
```
answer(bot, question, *, profile, context="", extra_roots=(), skills_override=None,
       on_tool=None, history="") -> {"answer": str, "refs": [str], "used": [item ids], "unsure": bool}
answer_question(bot, question, skills_override=None) -> str       # for the gate; profile "consult", no logging
system_prompt(bot, profile, skills_override=None) -> str
```
`profile` is `"chat"` or `"consult"`. The system prompt = role and rules, the bot's instructions, every
skill in full (or `skills_override`: a list of `{name,title,trigger,body}`), and `memory.index_text`.
The model is told to finish with a fenced JSON block `{"answer","refs","used","unsure"}`; the parser is
lenient (no block → the whole text is the answer, `unsure` false). Tools come from `readtools`; roots are
`resolve_reads(manifest) + [bot's skills dir] + extra_roots`.

### `experts/consult.py`
```
consult(bot, question, context="", caller=None) -> {"id","answer","refs","used","unsure"}
log(bot, limit=100) -> list[Consult]
Consult = {"id","ts","question","context","answer","refs","used","unsure","caller": {...}, "ms": int}
```
`caller` = `{"run_id","session_id","backend","target","canvas"}` as far as known. Its `target` folder is
added to `extra_roots` when it exists. At most 3 consults run at once per process (others wait). An unsure
answer writes an observation (`kind "gap"`, trust `gap`). Raises `model.Unavailable` when no engine is on.

### `experts/chat.py` — the bot engine for kind expert
```
run(bot, task, label=None) -> None        # the bots engine contract (bot.py start_task / _run_task)
```
Honours the engine contract in `docs/bots.md` §6 as mapped: "Task started: …" system line, `stop_flag`,
`_wait_if_paused`, `set_status`, `set_doing`, `done` / `error` events, `_routine_outcome`. Tool calls
become `action` events. Chat-only tools, executed in-process:
`remember_lesson {claim}` (memory.add trust `user`), `forget_lesson {text}`, `list_memory {}`,
`run_checks {}` (gate.start), `propose_skill {item_ids?: [..], topic?: str}` (proposals.prepare then the
card, raised inside this task thread with the `_gate` pattern).

### `experts/cards.py` — proposal cards outside a chat task
```
show(bot, proposal) -> bool          # raise the approval card when the bot is idle; False if it is busy
tick() -> None                       # for every expert: show the oldest "ready" proposal if idle
```
Pattern: `Bot.greet` (bot.py) — install a worker as `bot.thread` under `bot.lock`, emit `approval`
(`detail` = diff, plus `expert_proposal: <id>`, `score_before`, `score_after`), `set_status("waiting",
waiting_on=seq)`, wait on the inbox, classify with the YES / NO regexes, then `proposals.approve` /
`reject`, emit a `note`, set idle, release the thread, re-deliver any non-verdict text as a new task.

### `experts/capture.py`
```
capture_session(bot, session) -> list[Obs]
due_sessions(now=None) -> list[session]       # idle >= IDLE_S (1800), not captured at this length, not a bot session
relevant(bot, session) -> bool                # consulted this expert, or matches offered_to
IDLE_S = 1800
```
`session` = `{"session_id","backend","target","canvas","path","last_active"}`. One `model.text` call per
(session, expert): transcript as markdown (tail, capped at 30k chars), that session's consults, current
memory claims → JSON list of `{"kind","claim","about"}`. Zero observations is a normal result. The
watermark (`experts-captured.json`: `session_id -> {"size": transcript bytes, "at": ts}`) makes a resumed
session capture only once per new length.

### `experts/scheduler.py`
```
start() ; shutdown() ; tick() -> dict
TICK_S = 300
```
One daemon timer thread (`canvas_mirror` pattern). `tick`: capture due sessions for relevant experts → for
each expert `consolidate.run` when `observations.due` → for `proposals.candidates` not yet proposed:
`proposals.prepare` → `cards.tick()`. Never raises.

### `experts/seed.py`
```
seed() -> list[str]          # ids of experts created now
SHIPPED_DIR                  # fused_render/experts/shipped/<key>/{expert.json, instructions.md, skills/*.md, questions.md}
```
Creates each shipped expert once (witness lists keys already seeded, so a deleted expert stays deleted).

### `experts/routes.py` (FastAPI router, section 8)

## 5. Bot-kind integration (existing files)

- `bots/bot.py`: `KINDS += ("expert",)`, `is_expert`; `create()` accepts the kind (no greet model call:
  a fixed greeting `done` with `source="seed"`); `start_task` picks `experts.chat.run` for an expert before
  `_engine_for` and skips the `headed()` check; `_engine_for` never returns `agent`/`steps` semantics that
  launch Chrome for an expert; `clone()` refuses an expert; browser entry points (`wake_browser`,
  `takeover`, `popout`) refuse; `summary()` reports `browser: {"running": False}` shape-compatible and
  includes `expert: manifest` plus `expert_available` / `expert_reason`; Super Bot's hand-off targets and
  BOTS section exclude experts.
- `bots/routes.py`: create accepts the kind; browser ops refuse for an expert with a sentence.
- `bots/store.py`: `"expert"` in `PUBLIC_KEYS`. `bots/botfile.py`: keep `kind == "expert"` on import.
- `bots/registry.py`: a seed step calling `experts.seed.seed()` where `seed_super` runs.
- An expert keeps its inert `Browser` object (plain paths); nothing may call `browser.start()`.

## 6. The consult tool for task sessions

`claude_agent/permission_server.py` gains `expert_consult {expert, question, context?}` (the S3 tools are
the precedent: env switch `FUSED_RENDER_EXPERT_ORIGIN`, roster entry, HTTP POST via `_server_http`).
It POSTs `/api/experts/consult {expert, question, context, run_id}` and returns the answer as text:
the answer, then `Files:` refs one per line, then `(unsure)` when unsure.

`claude_agent/agent.py`: stamp the env switch in `_write_mcp_config` when at least one expert exists and is
available; Claude pre-allows `mcp__fused_render__expert_consult`; Copilot passes an allow for it; Codex
raises `tool_timeout_sec`; `_appended_prompt` gains a note listing the experts offered to this session
(name + `ask_me_about`), in each backend's tool spelling. The tool is offered only to sessions an expert's
`offered_to` matches.

## 7. Trust rules (load-bearing)

- A consult's `question` and `context` are data from an agent. They never write memory.
- Only `remember_lesson` in chat (the user's own words, at the Mac) writes memory directly.
- Capture extracts only: a pattern the user asked for, a user correction, an error then a fix that worked,
  expert advice confirmed or contradicted. Text the session merely read is never a lesson.
- A skill is only ever written by `proposals.approve`, after the user's Approve on the card.
- Expert answers are reference material for the caller; the tool description says so.

## 8. HTTP (`experts/routes.py`, prefix `/api/experts`)

POSTs require the `X-Fused` header (`server.common._require_fused`). Errors are `{"error": "<sentence>"}`.

| Route | Body / answer |
|---|---|
| `GET /api/experts` | `[{id, name, ask_me_about, engine, model, available, reason}]` |
| `POST /api/experts/consult` | `{expert, question, context?, run_id?}` → `{id, answer, refs, used, unsure}`; 404 unknown expert; 409 `{error, reason:"unavailable"}` |
| `GET /api/experts/{id}/memory` | `{items: [Item], index: str}` |
| `POST /api/experts/{id}/memory/forget` | `{id}` → `{removed}` |
| `GET /api/experts/{id}/observations` | `{pending: [Obs], due: bool}` |
| `GET /api/experts/{id}/consults` | `{consults: [Consult]}` (newest first, 100) |
| `GET /api/experts/{id}/checks` | `{latest: Run|null, status, questions: int}` |
| `POST /api/experts/{id}/checks/run` | `{run_id}` |
| `GET /api/experts/{id}/proposals` | `{proposals: [Proposal]}` |
| `POST /api/experts/{id}/consolidate` | `{force?: bool}` → consolidate result + `{proposed: [ids]}` |
| `POST /api/experts/{id}/capture` | `{session_id}` → `{observations: [Obs]}` |
| `POST /api/experts/{id}/settings` | `{engine?, model?}` → manifest |
| `GET /api/experts/{id}/engines` | `[{id, label, enabled, models: [{id, label}], default_model}]` |

## 9. `questions.md`

```
## How do I read the output of one UDF in another?
- must: mentions referencing the upstream UDF by name
- must: points at a real canvas file as an example
```
A `## ` line starts a question; each `- must:` line is one required point.

## 10. Frontend (`frontend/src/apps/bots/`)

`kind: "expert"`; an EXPERT badge; no browser pane, live view, tabs or "browser off" wording. The preview
column renders `ExpertPane` with tabs Memory · Consults · Checks (also reachable from Settings as an
"Expert" tab, which additionally holds Engine and Model). Proposal cards are ordinary `approval` events
with `expert_proposal`, `score_before`, `score_after`; the diff is the card body. Approve / Reject go
through the existing `/api/bots/<id>/send`.
