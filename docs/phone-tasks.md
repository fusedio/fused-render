# Tasks on the phone

*The iOS app's Tasks section: `fused_render/server/routers/phone_tasks.py`,
`fused_render/lan.py`, `frontend/src/lan/tasks/`, `ios/` (2026-11-05, D1464).*

## 1. What it is, and the shape

The Fused Render iOS app (`ios/`) is a thin native shell around the LAN
listener's page (`lan.py`, `frontend/lan.html`). It showed Apps and Bots
(docs/bots.md §14); it now shows a third section, Tasks: the computer's agent
tasks as a list at `/tasks` and one task's conversation at `/tasks/<key>`. It
is built exactly the way Bots is, in three parts:

- **A phone door on the server.** `/api/phone/tasks`, its own FastAPI router
  (`server/routers/phone_tasks.py`), included from `server/app.py` beside the
  tasks router. It serves trimmed rows (`phone_task`), a reduced transcript
  (`_turns`), the one card whose buttons are live (`_card`), and a fixed set
  of verbs (`_OPS`), each of which calls the desktop's own handler.
- **Phone-first lan pages.** `frontend/src/lan/tasks/`: `TasksList.tsx` (the
  list), `TaskView.tsx` (one task), `NewSheet.tsx` (the "+" sheet, Task and
  New app tabs; also opened from the Apps grid's "+"), `lib.ts` (pure helpers:
  lanes, chips, search, status words, `mergeTurns`), `api.ts` (typed fetch
  wrappers) and `types.ts` (the wire shapes below). Same page as the grid and
  the bots, its own pathname router; `SectionSwitch` is now
  `Apps | Bots | Tasks`, with the needs-you count as a badge on Tasks.
- **A thin native shell.** A Tasks place beside Apps and Bots, its title and
  back button, a row in the menu, a home-screen quick action, and deep links
  `fusedrender://open?host=…&page=tasks` (the list) and `…&task=<key>` (one
  task). Nothing in it knows the wire; it opens pages.

**Why not the desktop routes.** `/api/tasks/*` and `/api/claude/agent` stay a
404 on the LAN. `GET /api/tasks` carries every row with its three newest
messages — a 1.7 MB answer on a busy machine, polled every few seconds over
Wi-Fi. And the desktop's verbs include erase, worktree removal, terminal
hand-off, snapshot revert, app-state reads and mode switches: operations on
the Mac's screen and the Mac's disk. The door serves what a phone draws and
nothing else, and every write goes through the same desktop handler the Mac
uses, so the phone can never do something the Mac would refuse.

**On the LAN** (`lan.py`). `_PHONE_TASKS = "/api/phone/tasks"` is forwarded
for a paired device on `GET` and `POST` only (`_PHONE_TASKS_METHODS`; every
verb is a POST, so DELETE and PUT are 404), through `_AsDevice` like the bots
door: a copy of the scope with any client `x-fused-device` header dropped and
`X-Fused-Device: <id>` set from the pairing record. A path with a `.` or `..`
segment is a 404 before forwarding. `_TASKS_PAGE` (`/tasks` and `/tasks/<one
segment>`; a `pending:<entry>` key's colon arrives raw from the native shell
and percent-encoded from the page's own links, and both are one segment) joins
`/`, `/index.html` and `_BOTS_PAGE` in `_lan_page`: a paired GET serves
`shell-dist/lan.html` (503 when it is not built), an unpaired one the
how-to-pair page. On loopback (D3: trusted) the routes answer too; the header
is then optional and the device is "".

## 2. The wire contract (`/api/phone/tasks`)

Keys are the listing's: a session id, or `pending:<entry id>` for a message
that has not run yet. A key rekeys onto the session once the run mints one;
the phone follows `entry_id` across the swap (the page opens
`/tasks/<key>?entry=<entry id>` after a create).

| Route | Query / body | Answers |
|---|---|---|
| `GET /api/phone/tasks` | `gen`, `archived`, `limit` | `{tasks: [PhoneTask], total, generation, projects, attention, ts}`, or `{unchanged: true, generation}` |
| `GET /api/phone/tasks/{key}` | `since`, `last`, `before`, `limit` | `{task, turns, cursor, first, total, reset, live, run_id, cards, card, scheduled, ts}` |
| `POST /api/phone/tasks` | `{prompt, target}` | `{key, entry_id, task_id}` |
| `POST /api/phone/tasks/new-app` | `{prompt, name?}` | `{key, entry_id, task_id, app: {name, path, url}, task_error?}` |
| `POST /api/phone/tasks/{key}/{op}` | per op, below | the desktop handler's answer |

**The list** (`phone_tasks`). Drafts are never listed; archived rows only with
`archived` truthy (`1`, `true`, `yes`), and then beside the others. Rows are
newest activity first (`_activity`: `happened_at`, else `last_active`, else
`started`). `limit` is 300 by default (`LIMIT_DEFAULT`), clamped to 1..1000
(`LIMIT_MAX`); `total` counts every row shown before the limit. `projects` is
the folder facet over the rows shown — `{path, label, count}`, the path
tilde'd, most rows first; `attention` is the count of rows whose status is in
`ATTENTION_STATUSES` (`needs_attention`, `blocked`), the badge.

`generation` is a change token (`_token`): `<snapshot generation>.<digest>`,
the digest a blake2b over each row's key, `row_hash`, status, `live`,
`next_run`, `queue_position` and `unread`, and over the `archived` switch. The
generation alone is not enough: the builder's floor rebuilds keep it and are
exactly what move `live` and `next_run`. When `gen` equals the token the
answer is `{unchanged: true, generation}` and costs the digest only — no
filter, sort or trimming. `TasksList` polls every 5 s while visible with the
last token and leaves the DOM alone on `unchanged`.

**One task** (`phone_task_detail`). `turns` are the transcript's, reduced by
`_turns`, each with an index `i` and a stable `id`. `limit` is 80 by default
(`TURNS_DEFAULT`), clamped to 1..400 (`TURNS_MAX`).

- No `since`: the last `limit` turns.
- `since=<cursor>&last=<id>`: every turn from `cursor - 1` on, capped at
  `limit`. The last turn the phone holds is re-sent so a reply still streaming
  grows in place (the phone replaces by `i`, `mergeTurns`). If the turn at
  `cursor - 1` is not the one `last` names, or the cursor is past the end
  (`_history` drops a resolved error turn, a "Stopped" mark moves, the
  transcript shrank), `reset: true` and the last `limit` turns: the phone
  starts over.
- `before=<i>`: the `limit` turns before index `i` ("Load earlier"). `cursor`
  is then `total`: paging back never moves the forward cursor.

`first` is the index of the first turn sent, `cursor` the index after the last
one, `total` the transcript's length. `live` / `run_id` are the live run's
(`_history`'s `live_run`). `cards` are that run's permission and question
records on every poll (they carry their own verdicts and they are few);
`card` is the first whose verdict is `pending` — the one with live buttons —
or null. With no live run both are empty. `scheduled` are this key's
scheduled entries still `schedule.PENDING`. A `pending:` key answers its
`task` and `scheduled` and no conversation; so does a row with no target or a
server with no agent module. A transcript that cannot be read is an empty
conversation, not a 500. `TaskView` polls every 3 s while live and 10 s
otherwise.

**Create** (`phone_create`) and **new app** (`phone_new_app`): §5 and §6.

**The verbs** (`phone_op`, `_OPS`). Every one is a POST with `X-Fused: 1`;
the body is optional (anything not an object is `{}`). The op is checked
first (unknown: 404 `unknown task action '<op>'`), then the key (404 `no task
with key '<key>'`).

| Op | Body | Goes to |
|---|---|---|
| `send` | `{text}` | `api_task_send(key, {"text": text})` → `{queued, key}`. No model, effort or backend from the phone |
| `cancel` | — | `api_task_cancel` → `{cancelled: true}` |
| `decide` | `{request_id, decision, note?}` | `api_queue_decide` with `scope: "once"` → `{held, decided}` (§7) |
| `answer` | `{request_id, answers, custom?, note?}` | `api_queue_decide`, decision `allow`, `scope: "once"` (§7) |
| `archive` | — | `api_task_archive`; 409 `Stop the run first` (`RUN_BLOCKED_HINT`) while the row is `in_progress` (the desktop's own gate, `eraseBlocked`); a parked `needs_attention` row is filed after its run is stopped, as the desktop does |
| `unarchive` | — | `api_task_unarchive` |
| `rename` | `{title}` | `api_task_rename` |
| `favourite` | `{on: true\|false}` | `api_task_favourite`; a non-boolean `on` is a 400 |
| `delete` | — | `api_task_delete` (its own 409 while running) |
| `read` | — | `api_task_read` with `all=True` |
| `skip` | — | `api_queue_skip({"key": key})` → `{ok, position}` |

**Errors** are `{"error": "<sentence>"}`: a POST without `X-Fused: 1` is 403
(`_require_fused`); an unknown key or a verb the phone never gets is 404; a
malformed query number is 400 (`limit must be a whole number`); a verb's own
refusal (`ValueError`/`RuntimeError` raised in the op) is 400; a desktop
handler's `HTTPException` keeps its status and sentence (`_guarded`), and a
`JSONResponse` it returns is handed through as is.

**Not on the phone, on purpose.** Erase, export, worktrees, ports, holds,
running/idle marks, settings, queue admit and force, `pr-status`, `seen`,
`ui`, snapshots, the terminal: each is a 404 through the prefix. A decision
is always `once` — "allow for this session" and "always" are desktop
decisions. A task created from the phone always asks (§5); there is no auto
mode, and no model, effort or backend picker: the Preferences default agent
answers.

## 3. Rows — PhoneTask (`phone_task`)

The desktop `Task` cut to what the phone draws. Paths are tilde'd (`_tilde`),
labels are basenames (`_label`), and every free-text string is flattened to
one line and capped at 200 characters (`TEXT_MAX`, `_flat`).

| Field | |
|---|---|
| `key`, `task_id`, `entry_id`, `bot` | identity; `entry_id` survives the rekey |
| `title` (200), `title_source` | |
| `status`, `failed`, `blocked_reason`, `live`, `unread`, `starred` | |
| `attention` | `{tool, summary (200)}` or null |
| `project`, `project_label`, `target_label` | |
| `entry_url` | `/render?path=<entry>` when the folder opens as an app, else "" |
| `repo`, `prs` | `prs`: `[{repo, number, url}]` |
| `backend`, `model` | shown, never set from the phone |
| `last_message` | `{role, text (200), at}` or null |
| `last_reply` (200) | |
| `started`, `last_active`, `happened_at`, `message_count` | |
| `next_run`, `next_run_repeats`, `queue_position`, `queue_ahead_title` (200) | |

## 4. Turns, cards, scheduled

**PhoneTurn** (`_turns`). Every turn has `i`, `id`, `kind` and `at`.

| Kind | Fields | Made from |
|---|---|---|
| `user` | `text` | a prompt; line breaks kept (`_cut`), capped at 20000 (`TEXT_CAP`) |
| `assistant` | `text` | reply prose, line breaks kept, 20000; drawn markdown-lite |
| `tool` | `name`, `summary`, `status`, `detail` | one call and its result. `summary` is the desktop's `agent._tool_detail` or a per-tool fallback (`_tool_summary`: Bash's description or command, a file tool's basename, Glob/Grep's pattern, WebFetch/WebSearch's url or query, Agent/Task's description), its detail flattened to 120. `status` is `running`, `ok` or `error` (anything else reads `ok`); `detail` the output, line breaks kept, 600 (`DETAIL_MAX`), "" when the output is not a string |
| `system` | `text`, `tone` | an error (`tone: "error"`), a notice (its status, else `info`), "Stopped", "Context compacted"; text one line, 600 |

An assistant record whose segments interleave prose and tool calls becomes one
phone turn per run of prose and one per tool call, in order. Thinking,
subagent children, images and context meters are dropped. The `id` is the
transcript record's uuid, with `#<k>` for the k-th run inside a segmented
record and `#stop` for its "Stopped" mark; an error turn, which has no uuid,
is `err:` plus a hash of its text; a compaction with no uuid is `compact`;
anything else with no uuid is `n<i>`.

**PhoneCard** (`_card`). `{request_id, kind, tool, summary, detail, risk,
verdict, held, created_at, questions, answers}`. `kind` is `question` for the
agent's `ANSWERABLE_TOOL` (AskUserQuestion), `plan` for `PLAN_TOOL`
(ExitPlanMode), else `permission`. `summary` is the desktop's attention line
(`_attention_of`), 200. `detail` is the plan, a Bash command (both line breaks
kept, 600), or a file tool's tilde'd path; else "". `risk` is
`{severity, summary}` when the record has a severity, else null.

`verdict` is `allow` or `deny` for a decided card, `expired` for any other
decision, and for an undecided one `held` or `pending`. A **held** card is
answered: the project queue is holding the answer until the folder frees. It
is never the live card, and `decide`/`answer` refuse it — a second answer from
the phone would race the queue's onto the disk.

`questions` (question cards only, else null): `[{header (60), question,
multiSelect, options: [{label, description (200)}]}]`. `question` and `label`
go out **exactly as asked**, uncut and unfolded: the agent matches an answer to
them (`_answers_from`), and a cut would turn every answer into a deny.
`answers` is the record's own answers, at most 20, keyed by the exact question text (the phone looks them up by the question it drew), each value cut to 120.

**Scheduled** (`_scheduled`). `{entry_id, text (600, one line), due, state,
repeats}`.

## 5. Create (`phone_create`, `_known_folder`)

`{prompt, target}`. An empty prompt is 400 `prompt: required`. `target` must
be a folder the phone may start a task in (`_known_folder`), else 400
`target: pick a folder from the list` — the phone picks from the facet, it
never types a path:

- the `project` of a row the listing knows, in its own spelling (so the task
  joins that folder's queue — `project_queue` keys on the path as written,
  never its realpath); or
- a directory strictly under the workspace (`fused_dir()`) whose real
  location stays inside it — a symlink out of the workspace is refused.

Never `/`, the home directory or the workspace root itself; never a path with
a dot-directory segment; never something that is not a directory. A tilde is
expanded.

The task is created through `tasks._create_task(body, None, origin="phone")`
with only `prompt`, `target` and `permission_mode: "default"` — which the
page mode table (`_PAGE_PERMISSION_MODES`) stores as `prompt`, the asking
mode. Any mode, model, effort or backend in the phone's body is ignored, so
the Preferences default agent runs it. The answer is the pending key, the
entry id, and the row's `task_id`.

## 6. New app (`phone_new_app`, `_new_app_sync`)

`{prompt, name?}`. An empty prompt is 400; a `name` that is not a string is
400. With no name, the computer names it:

1. `_suggest_name`: one call to the AI relay (`ai._ai_relay`) on `haiku`
   (`NAME_MODEL`) with `NAME_SYSTEM_PROMPT` — the desktop hero's own naming
   prompt (`apps/builder/HomeHero.tsx`), kept the same — and a 12 s timeout.
   The reply must be kebab-case and not `none`; it is cut to four words
   (`_kebab`). A relay that is down, slow or answers prose gives "".
2. Else `_dated_slug`: `app-YYYYMMDD-HHMM`.
3. `_free_name`: that name, or `-2`, `-3`, … — the first the workspace's
   `app/` folder does not hold. If the copy still finds it taken (two quick
   taps in a minute), the next free one, once.

A **typed** name is never suffixed: an invalid one is 400 (`_app_name_error`),
a taken one the desktop's 409, so the person picks another. Then the desktop's
own `apps._new_app(body, permission_mode="prompt", origin="phone")` copies the
starter, inits git, stamps the app id and starts the build task on the entry
html with the default agent — asking, never unattended. It runs in a thread
(`asyncio.to_thread`): `_new_app` waits up to 20 s for the task's send. The
answer's `key` is the session if the send already minted one, else
`pending:<entry>`; `app.path` is tilde'd, `app.url` is
`/render?path=<entry html>`; `task_error` is passed on when the app was made
but its task was not.

## 7. Deciding and answering (`_decide`, `_answer`, `_decide_with`)

Both look the card up server-side (`_live_cards`): the row's live run, read
through `agent._history_live`, and its permission records that are undecided
and not held. `request_id` must be one of them, else 400 `that card is not
waiting on an answer`. The run id and session id come from that read, never
from the body. `decide` takes `decision: "allow" | "deny"` and refuses a
question card (`that card is a question: answer it`); `answer` refuses a
permission card (`that card is a permission: allow or deny it`). `scope` is
always `once`.

**The answer payload** is the desktop composer's: `answers: {question: label
| [labels]}`, `custom?: {question: typed}`, each key the question text
exactly as the card served it. `_answer_values` turns it into what the agent
latches, one string per question: a string is passed as is; a list becomes
the chosen labels joined with `", "` **in the card's option order**, a typed
"Other" (present in the list and in `custom`) last. A list matching no label
is joined as sent. Any other value is 400 `answers: each value is a label or a
list of labels` — a 400 here, not a silent deny in the agent. A missing or
empty `answers` is 400 `answers: required`; a `custom` that is not an object
is 400.

## 8. Security notes

- **The cookie already grants code execution.** A paired device can
  `POST /api/run`; this door gives it nothing it could not do otherwise. The
  scope is kept narrow anyway: a phone left unlocked on a table should be able
  to start, steer, stop and answer a task, not erase history, remove a
  worktree or grant a tool for good.
- **The device is the listener's word.** `X-Fused-Device` is stamped by
  `lan.py` from the pairing record; a client-sent one is dropped. The router
  accepts only an id shaped like one (`_DEVICE_RE`) and logs it on create and
  new-app.
- **A phone cannot claim to be anything else, and a page cannot claim to be
  the phone.** `origin` and the new-app `permission_mode` are arguments of the
  seams (`_create_task(body, x_fused_page, origin)`,
  `_new_app(body, permission_mode, origin)`), not body fields.
- **Only a card the detail served.** `request_id` must name an undecided,
  unheld card of the row's live run (§7); run and session ids are read on the
  server.
- **No blind POSTs.** Every POST needs `X-Fused: 1`; folders are chosen from
  the server's own list (§5); dot segments never forward.

## 9. Tests

- `tests/test_phone_tasks.py` — the router on a `TestClient`: trimmed rows
  newest first with the facet and the badge; `limit` and its validation;
  archived only on request; drafts never listed; the transcript reduced to
  phone turns and `_turns` on fixture segments; `_tool_summary`'s fallbacks;
  the live cards and the one live card; a `pending:` key; 404s for unknown
  keys and the desktop-only verbs (erase, export, worktrees, settings, admit,
  force, pr-status, seen, ui); 403 without `X-Fused`; create from a known
  folder only, stamped `origin: "phone"` and `permission_mode: "prompt"`;
  new-app naming through the relay, the dated slug, `-2`/`-3` and the race;
  send/cancel through the desktop handlers; archive's and delete's 409s;
  rename, star, read, delete; `decide` forced to `once` and refusing a card
  that is not live; `answer`'s joined payload checked against the agent's own
  `_answers_from`; skip.
- `tests/test_lan_phone_tasks.py` — the listener: unpaired is refused (401 on
  the API, the pair page on `/tasks`); `/tasks` and `/tasks/<key>` serve the
  lan page and nothing deeper does; the prefix forwards GET and POST with the
  record's device id over a spoofed one, and not DELETE or PUT; `/api/tasks`,
  `/api/claude/agent` and their neighbours stay 404; dot segments never
  forward.
- `frontend/src/lan/tasks/lib.test.ts` — the pure helpers under `bun test`.
