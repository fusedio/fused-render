# Tasks on the phone — design and plan

Status: proposal, 2026-11-05. Adds a Tasks section to the Fused Render iOS
app beside Apps and Bots: the list of the computer's tasks with filters, one
task's conversation, and the operations a person needs from a phone.

## 0. The shape of the thing (read this first)

The iOS app (`ios/`) is a thin WKWebView shell over the LAN listener
(`fused_render/lan.py`). Apps are the grid at `/`; Bots are `/bots` and
`/bots/<id>`, phone-first pages in the lan Vite entry (`frontend/src/lan/`)
over a **phone door** (`/api/phone/bots`, `fused_render/bots/phone.py`) that
the listener forwards for a paired device. The native side only gives those
pages a title, a back button, a menu row, a quick action and a deep link
(`RootView.swift`, `AppModel.swift`, `ManifestSync.swift`).

Tasks follow the same pattern, deliberately, and for the same reasons
docs/bots.md §14 gives:

- **Web pages, not SwiftUI.** `/tasks` and `/tasks/<key>` in the lan entry.
  The desktop Tasks page is not reused (eight views, the peek, the full
  Claude chat bundle); the lan entry already has the phone chat primitives
  (polling, viewport-aware composer, markdown-lite, cards, toast).
- **A phone door, not the desktop routes.** `/api/phone/tasks`
  (`fused_render/server/routers/phone_tasks.py`). The desktop `/api/tasks/*`
  and `/api/claude/agent` stay 404 on the LAN: they carry the whole 1.7 MB
  listing, erase, worktree removal, terminal hand-off, snapshot revert,
  `app_state`, mode switches — Mac-screen and Mac-disk operations. The door
  serves trimmed rows, a phone-sized transcript, and exactly the verbs below,
  with `X-Fused-Device` stamped from the pairing record so a send from the
  phone is attributable.

What the user gets: open the app → Tasks → see what is running, what needs
them, answer the card, send a follow-up, stop a run, file it, start a new one.

## 1. Scope

In (v1):

1. **List** at `/tasks` with search, status filter, project filter, "Needs
   you" shortcut, archived on request; sections by lane; unread marks; pull
   to refresh; polled while visible.
2. **Task** at `/tasks/<key>`: header (title, status, folder, agent, PR
   chips), the conversation (prompts, replies, tool lines, cards), the
   pending card with its buttons, a composer, Stop.
3. **Operations**: send a follow-up, stop the run, answer a permission card
   (allow once / deny), answer a question card, archive / unarchive, rename,
   star / unstar, delete (soft), mark read, cancel a pending scheduled
   message, skip the queue, create a task (prompt + folder from the
   recent-projects list).
4. **Native**: a third segment "Tasks" in the page's section switch; title
   and back button in the native top bar; "Tasks" in the ⋯ menu and as a Home
   Screen quick action; `fusedrender://open?host=…&page=tasks` and
   `&task=<key>` deep links.

Out (v1), with the reason:

- **Erase** (destroys the transcript, D307): a modal that "cannot be undone"
  belongs at the Mac. Soft delete only.
- **"Allow for session" / "Always allow"** on a permission card: the phone
  answers one card at a time, as it does for bots ("Forever never comes from
  the phone"). `scope` is forced to `once` server-side.
- **permission_mode `auto` on create**, model/effort pickers, plan mode,
  backend choice: a phone-created task runs under the Preferences default
  agent in `default` mode. v2 can add the pills.
- Drafts, the calendar, the board, worktrees, terminal hand-off, export,
  queue force / admit, PR status refresh, settings. Not phone work.
- Push notifications: there is no push infrastructure and the listener is
  LAN-only. A badge on the Tasks segment ("3 need you") while the app is
  open is what v1 has; a widget is v2 (§8).

## 2. Wire contract — `/api/phone/tasks`

Its own router (`fused_render/server/routers/phone_tasks.py`), included from
`server/app.py` beside the tasks router. Every POST needs `X-Fused: 1`
(`server.common._require_fused`); errors are `{"error": "<sentence>"}`; an
unknown key is 404, a refused verb 400, a verb the desktop has and the phone
does not 404. On loopback the routes answer too (D3), device "".

`key` is the listing key: a session id or `pending:<entry>`. The colon is a
plain path character; the lan listener's dot-segment check applies.

| Route | Does |
|---|---|
| `GET /api/phone/tasks?gen=<n>&archived=0\|1&limit=<n>` | `{tasks: [PhoneTask], generation, projects: [{path, label, count}], attention: n, ts}`. Rows from `tasks._listing()` (the built snapshot, no second build), drafts dropped, archived dropped unless `archived=1`, newest activity first, cut to `limit` (default 300, max 1000). With `gen` equal to the snapshot's generation and the archived flag unchanged: `{unchanged: true, generation}` — the 5 s poll costs nothing most ticks. `projects` is the folder facet, counted over the rows sent. `attention` is the count of `needs_attention` rows, for the badge. |
| `GET /api/phone/tasks/{key}?since=<cursor>` | `{task: PhoneTask, turns: [PhoneTurn], cursor, live, card: PhoneCard\|null, deleted?}`. The conversation through `agent._history` (the in-process road `/api/claude-sessions/history` takes), reduced to PhoneTurn (§3). `since=<cursor>`: only what landed after it (the transcript byte offset `_history` already returns). `card` is the one card whose buttons are live right now (a permission request or a question), from `_cards` / `_live_run`. `pending:` keys answer with the scheduled message as the only turn. |
| `POST /api/phone/tasks` | create `{prompt, target}` → `{key, entry_id, task_id}`. `target` must be one of the folders the listing knows (a `project` of an existing row) or under `~/Fused`; anything else is 400 — the phone picks from a list, it never types a path. Stored `origin: "phone"`, `permission_mode: "default"`, `queue` omitted. The row's key is `pending:<entry>` until the run mints a session; the phone follows `entry_id`. |
| `POST …/{key}/send` | `{text}` → `{queued, key}`: `api_task_send`'s path (a live host takes it; otherwise stored and sent now through the scheduler, `queued: true` when the folder is busy). No model/effort/backend from the phone. `pending:` keys are 409 as on the desktop. |
| `POST …/{key}/cancel` | the Stop button (`api_task_cancel`): interrupt then kill; on a `pending:` key, cancels the scheduled message. 404 `not_running` when nothing is live. |
| `POST …/{key}/decide` | `{request_id, decision: "allow"\|"deny", answers?, note?}` → the chat's `_decide` with `scope: "once"` forced, through the queue door (`api_queue_decide`) so a parked run's card is held the same way the desktop's is. The request must be the card the detail just served (`card.request_id`), else 400 "that card is not waiting on an answer". |
| `POST …/{key}/answer` | `{request_id, answers: {…}}` for a question card (AskUserQuestion), the same `_decide` with the answers payload the desktop composer sends. |
| `POST …/{key}/archive`, `/unarchive` | `api_task_archive` / `api_task_unarchive` by key; a running task is refused with "Stop the run first" (`RUN_BLOCKED_HINT`, same words). |
| `POST …/{key}/rename` | `{title}` → `api_task_rename` (empty clears; `pending:` refused). |
| `POST …/{key}/favourite` | `{on: bool}` → `api_task_favourite` without `open`. |
| `POST …/{key}/delete` | the soft delete (`api_task_delete`): cancels pending work, tombstones the key, transcript untouched. Running → refused. |
| `POST …/{key}/read` | `api_task_read` for the key (all messages) — opening a task marks it read, as the desktop peek does. |
| `POST …/{key}/skip` | `api_queue_skip` for a queued row. |

Not on the phone (404): erase, export, worktrees/remove, ports, holds,
running/idle marks, settings, queue admit/force, pr-status, seen, ui.

**On the LAN** (`lan.py`): `_PHONE_TASKS = "/api/phone/tasks"`, forwarded for
a paired device on GET/POST through `_AsDevice` exactly as `_PHONE_BOTS` is
(dot segments refused); `_TASKS_PAGE = re.compile(r"^/tasks(?:/[^/]+)?$")`
joins `_BOTS_PAGE` in the three places it is read (the page branch, the
unpaired how-to-pair page, the grid-cookie check). `/api/tasks` and
`/api/claude/*` stay 404.

**Attribution.** `schedule.create` / `api_task_send` take an `origin`; the
phone door passes `origin: "phone"` and the device id in the entry's `via`
(the shape `channels.base.phone_via` already uses for bots), so the desktop
row's message line can say "from your phone" later without a second
decision. Nothing in v1 draws it.

## 3. Row and turn shapes

`PhoneTask` — the desktop `Task` (platform/lib/api.ts) cut to what the phone
draws, every string capped:

```
key, task_id, title, title_source, status, failed, blocked_reason,
attention {tool, summary} | null, live, unread, starred,
project (tilde'd), project_label (basename), target_label, repo,
prs [{repo, number, url}], backend, model,
last_message {role, text (200 chars), at} | null, last_reply (200 chars),
started, last_active, happened_at, message_count,
next_run, next_run_repeats, queue_position, queue_ahead_title,
entry_id, bot
```

`PhoneTurn` — what `_history`'s segments become on a phone screen. One
reduced vocabulary, built in the router, drawn by one component:

| kind | from | carries |
|---|---|---|
| `user` | a prompt (chat, scheduled, phone) | text, at, via (kind) |
| `assistant` | reply text | text (markdown-lite), at |
| `tool` | a tool call and its result | one line: the desktop's own summary (`Edit app.py`, `Bash: npm test`), ok/failed, a `detail` cut to 600 chars for a tap-to-expand |
| `permission` | a permission request record | request_id, tool, summary, risk {severity, summary}, verdict (`allow`/`deny`/`expired`/pending) |
| `question` | an AskUserQuestion card | request_id, questions [{header, question, options, multiSelect}], answer (when answered) |
| `system` | interrupted, usage limit, resumed, queue notes | text |
| `scheduled` | a message not yet run (pending/missed/cancelled) | text, at, state |

Thinking, subagent peeks, snapshots, artifacts, context meters, app-state
reads and attachments are dropped (`trace`-class on the bots door). An image
the user attached shows as "(image)". This is the one piece of new server
logic with any size to it; it lives in `phone_tasks.py::_turns()` with a
table test over a fixture transcript, and it never touches `agent.py`.

`PhoneCard`: `{request_id, kind: "permission"|"question", …the turn's fields,
held: bool}` — `held` when the queue is holding the run.

## 4. Screens (`frontend/src/lan/tasks/`)

Routes in `lan/bots/lib.ts::parseRoute` grow two members: `{name: "tasks"}`
and `{name: "task", key}`. `main.tsx` lazy-loads a `tasks/screens` chunk the
way it does `bots/screens`. `nav.tsx::SectionSwitch` takes a third item,
`Apps | Bots | Tasks`, with the attention count as a small badge on Tasks.

**TasksList** (`/tasks`)

- Header: section switch; count `shown/total`; a search field (title,
  last reply, folder, task id); a filter strip of chips: **Needs you**
  (status `needs_attention`+`blocked`), then the lanes **Running**
  (`in_progress`+`queued`), **Upcoming**, **Done**, **Archived**; then a
  **Folder** chip that opens a sheet listing `projects` with counts
  (multi-select, like the desktop's). Filters persist in `localStorage`
  under `fused-render:lan-tasks-filter` (the grid's own pattern); Archived
  is never persisted on.
- Body: sections in a fixed order — Needs you, Running, Queued, Upcoming,
  Done, Archived — each with the server's order inside (newest activity
  first; Upcoming soonest first, the one exception `tasks-lib.sortLane`
  names). A section header reads `Running · 2 running, 1 queued`.
- Row: a status dot (tone per lane, the bots' `TONE_COLOR`), the title
  (`cardTitleLine` rule: newest user message first line, else title), a
  caption (`folder · agent · 2h`), an unread pip, a star, a PR chip, and
  for a card waiting the tool's summary in the warning tone. Swipe actions:
  Archive (or Unarchive), Star. Long-press: the row's menu (Rename, Star,
  Archive, Delete).
- Empty states: not paired / can't reach (the grid's), "No tasks yet",
  "Nothing matches" with a Show everything button.
- A **+** button opens the **NewTaskSheet**: a textarea, a folder picker
  (the `projects` list, most recent first, with "~/Fused/local/<new>" at
  the bottom for a fresh folder), Run. On `{key}` the page navigates to
  `/tasks/<key>` and follows the rekey by `entry_id` (the list poll carries
  it).
- Poll: `usePoll` every 5 s while visible, with `gen`; a 304-style
  `unchanged` leaves the DOM alone.

**TaskView** (`/tasks/<key>`)

- Strip on top: status word and dot, folder, agent and model, PR chips
  (tap → the PR URL in Safari), queue position when queued ("2nd in line
  behind <title>" with a **Skip the line** button), next run when upcoming
  (with **Cancel** for the pending message).
- Thread: PhoneTurns, oldest first, grouped by day; the bots' `Thread.tsx`
  layout (user right, assistant left, tool lines compact, system centered).
  Loads the tail (last 80 turns) and pages older on "Load earlier"; polls
  `since=cursor` every 3 s while `live`, 10 s otherwise (`usePoll`).
- The card: a `permission` turn with no verdict renders its buttons —
  **Deny**, **Allow once** — with the risk pill and summary; a `question`
  turn renders its options (single or multi) and a Send. Only the card
  `card.request_id` names is live; an older unanswered one reads "answered
  at the Mac" / "expired". Answering holds the buttons until the poll
  shows the verdict (the bots' local-hold rule).
- Composer: the bots' `Composer.tsx` minus photos (v1); Send → `/send`;
  `queued: true` shows a toast "Queued — the folder is busy". While live, a
  **Stop** button sits beside Send. `pending:` keys show the scheduled
  message and no composer.
- Header ⋯ menu: Rename (inline), Star, Archive/Unarchive, Delete (confirm
  sheet; refused with the server's sentence while running), Open folder's
  app (when `entry` is an app: `/render?path=…` through the grid's own
  door).
- Opening the task posts `/read` once.

## 5. Native changes (`ios/`)

Small, and all of a kind with what Bots needed:

- `RootView.swift`: `Place` gains `.tasks` (`/tasks`) and `.task`
  (`/tasks/<key>`); titles "Tasks" / the page's `<title>` (the page sets it
  to the task's title); the left button on a task goes back to `/tasks`, on
  the list goes home; the ⋯ menu gains "Tasks" (`checklist`).
- `AppModel.swift` / `DeepLink`: `page=tasks` and `task=<key>` targets.
  A key is a session uuid or `pending:<entry>`; DeepLink admits
  `[A-Za-z0-9_:-]` and nothing else (the colon is new; it was `[A-Za-z0-9_-]`
  for bot ids).
- `ManifestSync.installQuickActions`: a "Tasks" item after "Bots".
- `WebView.swift`: nothing — the page is same-origin, the cookie and the
  pinned session already cover it. `onGridLoaded` keeps its meaning (the
  grid is still the pairing landing).
- `project.yml`: bump `MARKETING_VERSION` to 0.2.0 with the release.

## 6. Security notes

- The pairing cookie already grants `POST /api/run` (code execution on the
  computer, D1463's own words), so letting the phone drive agent tasks does
  not widen what a stolen cookie can do in kind. What is kept narrow anyway:
  no `auto` permission mode from the phone, no session-wide or permanent
  grants, no erase, no path typed by the phone (folders are chosen from a
  list and checked against the listing), no backend switch, and every write
  stamped with the device id.
- `_AsDevice` drops a client-sent `X-Fused-Device`; the router validates the
  stamped id with the bots' `_DEVICE_RE`.
- `request_id` on decide must equal the one the detail served for that key,
  and `_decide` itself refuses a malformed id (`_bad_id`), so a phone cannot
  answer another task's card by guessing.

## 7. Tests

- `tests/test_phone_tasks.py` (the `test_bots_phone.py` pattern, TestClient
  on the inner app): list trimming and `unchanged`; archived gating;
  `projects` facet; detail turns from a fixture transcript (every PhoneTurn
  kind, the 600-char cut, `since`); card = the live request only; decide
  forces `scope: "once"` and refuses a stale `request_id`; create refuses a
  folder outside the listing; send/cancel/archive/rename/favourite/delete/
  read/skip hit the desktop handlers; `X-Fused` guard; 404 for erase/export/
  settings.
- `tests/test_lan_phone_tasks.py` (the `test_lan_phone_bots.py` pattern):
  `/tasks` and `/tasks/<key>` serve the lan page when paired and the
  how-to-pair page when not; `/api/phone/tasks` forwards with the device
  stamped; `/api/tasks` and `/api/claude/agent` stay 404; dot segments
  refused.
- `frontend/src/lan/tasks/lib.test.ts` (bun): `parseRoute` for the new
  routes; filter and section logic; the title rule; time words.
- iOS: `DeepLink` parsing of `page=tasks` / `task=<key>` with the colon; a
  manual pass on a device (pair, list, open, answer a card, send, stop).

## 8. Plan

Each step is shippable on its own and lands as its own PR.

1. **Server door, read side** (~1 day). `phone_tasks.py` with `GET` list and
   detail, `_turns()` and its fixture test, `lan.py` page and prefix wiring,
   the two test files. Reviewable without any UI: curl on loopback.
2. **Server door, write side** (~0.5 day). create, send, cancel, decide,
   answer, archive, unarchive, rename, favourite, delete, read, skip, plus
   `origin: "phone"`. Tests.
3. **Lan page: list** (~1 day). Routes, section switch, `TasksList`, filter
   strip, folder sheet, new-task sheet, polling with `gen`.
4. **Lan page: task** (~1.5 days). `TaskView`, thread renderer for the seven
   turn kinds, cards with buttons, composer and Stop, header menu, read
   mark.
5. **iOS** (~0.5 day). `Place`, titles, menu row, quick action, deep link,
   version bump. Device pass.
6. **Docs** (same PRs). A §15-style section in `docs/bots.md`'s sibling — a
   new `docs/phone-tasks.md` holding §2–§3 of this file as the wire record —
   and a DECISIONS.md entry: "Tasks on the phone are a phone door and a lan
   page, not the desktop routes".

v2 candidates, in the order they came up: photos in the composer (the bots'
own `files` path), model/effort pills on create and send, a Home Screen
widget "needs you" count (`ManifestSync` already refreshes from the grid
load; a `/api/phone/tasks?limit=0` adds the number), export → share sheet.

## 9. Open questions

1. **Create from the phone at all in v1?** It is the one verb that starts
   work on the computer from a prompt typed elsewhere. The spec includes it
   because Bots already allow it; dropping it costs one sheet and one route.
2. **Done tasks: how many?** The desktop lists everything; a phone list of
   800 rows is a scroll, not a list. The spec caps at 300 newest with
   Archived off by default. If people want the archive searchable, add
   server-side search to the list route rather than raising the cap.
3. **Question cards with free text.** AskUserQuestion's "Other" needs a text
   field in the card; the spec includes it in `/answer` but the UI may want
   it in v2.
