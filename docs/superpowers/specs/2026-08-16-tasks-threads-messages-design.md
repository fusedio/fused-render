# Tasks, threads, and messages

A rename that is really a remodel. Today the app has two parallel worlds — a
*scheduled message* store it owns, and a *Claude session* store Claude Code
owns — joined by one field. This design collapses them into one noun.

> **Task = Claude session.** Always, 1:1.
> A task has a **thread**. A thread has **messages**.

What we call a "task" today (a prompt plus a due time) is not a task in the new
model. It is **one scheduled message**.

---

## 1. The model

```
PROJECT  = a folder  (~/Desktop/fused/fused-render)
│
└─ TASK-002                       ← one Claude session, one thread
   │  title · status · live? · unread count
   │
   ├─ MSG-003  "pull today's news"   09:00 today   ● unread
   ├─ MSG-002  "pull today's news"   09:00 yest
   └─ MSG-001  "pull today's news"   09:00 Mon
```

A message enters a thread from one of three places, and the thread does not
care which:

* the Claude chat in the explorer,
* the Claude template chat,
* a schedule, from the Tasks page.

Everything is newest-first: newest message at the top of a thread, newest task
at the top of a list.

### Rename map

| today | new |
| --- | --- |
| Claude session | **Task** |
| scheduled entry | **Message** (a scheduled one) |
| `entry.message` | the message body |
| `entry.claude_session_id` | which task the message belongs to |
| `recurring` template | a **recurring message** |
| Inbox page | *deleted* — Tasks absorbs it |

---

## 2. Project = the folder

**Verified**, `fused_render/templates/claude/agent.py:491`:

```python
def _workdir(file: str) -> str:
    return file if os.path.isdir(file) else os.path.dirname(file)
```

Every transcript lands in `~/.claude/projects/<munged cwd>/`, and that cwd is
always a **directory**. A task targeting `~/x/foo.py` writes into the same
bucket as one targeting `~/x`. Files and folders do **not** get separate
session pools.

```
~/x/foo.py  ──┐
              ├──▶  ~/.claude/projects/-Users-x/     one bucket
~/x/        ──┘
```

The one thing that *is* per-target is the sidecar
(`home_dir()/sidecar/<mapped path>.json`), which keeps a `claudeSessions` list
per file and per folder. That list is a lookup aid, not a grouping: the
template's own docs note it is deliberately narrower than the real set, because
terminal chats in the same cwd claim no sidecar.

**Decision: a project is the folder.** A task on `foo.py` belongs to project
`~/x`, and keeps `foo.py` as its displayed *target*. This gives the grouping for
free (the store already groups that way), keeps the project filter short, and
avoids the ambiguity where one session would belong to two projects at once.

---

## 3. Identity

### Task IDs

`TASK-001`, `TASK-002`, … numbered **per project**, so each folder starts at
001.

* Allocated **once**, at task creation, and **never renumbered**. Deleting
  TASK-001 does not promote TASK-002.
* Backfilled once over existing sessions, in creation order per project.
* Stored in a new global `task_ids.json` (see §9).

### Message IDs

`MSG-001`, `MSG-002`, … **per task**, in send order.

---

## 4. Title and description

Claude Code already writes the one-liner we want. **Verified** in a real
transcript:

```json
{"type":"ai-title","aiTitle":"Build flight details analyzer with Fuse renderer","sessionId":"b75898b0-…"}
```

It is re-emitted every turn (242 copies in the transcript sampled), so the title
tracks the conversation as it evolves. **Take the last record.**

Title precedence:

1. the user's own `title`, if set,
2. `aiTitle` from the transcript,
3. the first line of the first message.

No summarisation call is needed anywhere.

`description` is a new optional free-text field on the create form, **empty by
default**. Note that `aiTitle` is a title, not a summary — Claude Code stores no
summary — so description is not auto-filled in this pass. Deferred; see §11.

---

## 5. A scheduled task that has not run yet

A session id does not exist until Claude Code's first turn mints one, so a task
scheduled for tomorrow has no session. Three shapes were considered:

| | A — pending task, lazy session | B — message only, task on first run | C — pre-spawn a session |
| --- | --- | --- | --- |
| row in List/Board before the first run | yes | **no** | yes |
| Board "Upcoming" column | works | **always empty** | works |
| task is always a real session | no, until it runs | yes | yes |
| cost | none | none | a process spawned days early |

C is not viable: an empty session writes no transcript, so there is no id to
hold. B empties the Upcoming column, which is most of the point of the Board.

**Decision: A.** The task row exists from creation with an empty session id,
which is what the store already does today (`claude_session_id: ""`). The task
ID is allocated at creation. The session id fills in on the first run.

---

## 6. Repeat

Recurrence belongs to the **message**, not the task. A recurring message
re-sends the same body at each occurrence, into the same thread.

* Because a task *is* a session, chaining is now the default by construction —
  there is no separate "chain" flag to design.
* The opt-out checkbox therefore means something cleaner than before:
  **"new task each run"**. Opting out mints a fresh task (and so a fresh
  session) per occurrence, rather than producing an orphan session inside an
  existing task.
* A task may hold more than one recurring message — a daily one and a weekly
  one — plus ad-hoc follow-ups. Nothing special-cases this; it falls out of
  "a task is a bag of messages".

### Form layout

Repeat moves behind a quiet checkbox next to the time. The repeat dropdown only
appears once it is checked; editing an existing repeating message opens checked.

```
BEFORE                          AFTER
🕐 [date] [time]                🕐 [date] [time]   ☐ Repeat
   [Repeats ▾]   ← always
                                (checked)
                                   [Repeats ▾]  ☐ New task each run
```

---

## 7. Unread

Unread means: **a message whose response I have not seen.**

* Tracked per message, not per task. A task shows an unread count.
* Clicking a message opens the explorer's Claude chat and **scrolls to that
  message**. That click is what marks it read.
* A running task bumps to the top of the list, which is how a chained run
  surfaces itself.

---

## 8. The three views

The tab bar order becomes **List · Board · Calendar** (List first).

### List

Tasks, each an accordion.

```
▾ TASK-002   Pull today's news        ~/Desktop/fused    ● 2
    MSG-003  "pull today's news"   09:00 today      ● unread
    MSG-002  "pull today's news"   09:00 yesterday
    MSG-001  "pull today's news"   09:00 Monday
    ⌄ show more
```

* The three most recent messages, newest first; **Show more** loads the rest.
* A task with one message shows one sub-item and no Show more.
* Every task appears here — scheduled and pure-chat alike.

### Board

Columns of **tasks**, not messages. A task's status is derived from its
**newest** message.

A consequence worth stating: a live recurring task always has a pending newest
message, so it sits permanently in Upcoming and never reaches Done. That is
correct — the task is not finished.

### Calendar

Only **scheduled** work appears; a chat message has no time.

The chip **is a task**, not a message — the same unit the other two views show.
It carries the task's title, the task's colour, and opens the task's thread.
What the time axis adds is placement:

> **One chip per task per day**, anchored at that task's **earliest** message
> that day. Any later message the same day nests inside it, and the anchor chip
> carries the count.

```
        MON 17            TUE 18            WED 19
 5 AM  ▍Pull news  +1     ▍Pull news        ▍Pull news
       └ also 7 PM
 9 AM  ▍Review PRs                          ▍Review PRs
```

* Clicking a chip opens a popover listing the task's thread, that day's messages
  first, with their real times.
* Chips of the same task share **one colour** across the grid, so five days of a
  daily task read as one thing. Recurring chips carry a small `↻`.
* Unrelated tasks are unrelated chips. Three tasks at 9am / 2pm / 6pm are three
  chips.
* Chips are **fixed one-line height** — a message has a start time and no
  duration, so there is nothing for a variable height to encode. This matches
  how Google Calendar renders a short event.
* An hourly recurring message still produces one chip per day, not 24 — the
  anchor sits at the first run and carries `+23`.

**The known cost of this rule, accepted deliberately:** a task's 7pm run has no
chip at 7pm. The `+N` badge and the `also 7 PM` sub-line on the anchor are the
mitigation — the later runs are named, just not placed. The alternative (a chip
per message) was rejected because it shows one task many times in a single day,
which the other two views never do.

#### New: 4-day view

Alongside the existing week view, modelled on Google Calendar's:

* the current day is always leftmost, with the three following days after it,
* the arrows step **four days** at a time,
* **Today** snaps back to today-leftmost.

Its value is width: four columns are wide enough to read a task title.

---

## 9. Missed messages and the queue

### What happens today

**Verified** in `fused_render/schedule.py`:

```
app closed         →  nothing fires. period.
app opens          →  wall-clock sweep; past-due fires IF within its bound
   one-off bound   =  24 h    (_DEFAULT_MAX_LATE_S)   → caught up
   recurring bound =  120 s   (_OCCURRENCE_MAX_LATE_S) → skipped, marked missed
past the bound     →  state = missed, visible, never sent
creating in past   →  REFUSED beyond the bound (schedule.py:535)
```

Nothing counts elapsed ticks, so catch-up is what the absence of tick-counting
gets for free. The bounds are the deliberate part.

### What it becomes

**Missed work is queued and runs when the app next opens.**

* **Catch-up is unbounded.** `_DEFAULT_MAX_LATE_S` no longer stops anything.
  (Chosen knowingly, revisitable — the cancel affordance below is what makes it
  safe.)
* **Non-repeating messages: run all of them.** Ten one-offs missed over two
  weeks all fire on open.
* **Repeating messages: coalesce.** Only the *latest* missed occurrence runs;
  the rest are dropped and reported ("5 runs skipped"). Replaying a week of
  "daily at 9am" into one thread is not what the words meant, and this is the
  surviving half of today's 120-second rule.
* **Scheduling into the past is allowed.** The guard at `schedule.py:535` is
  removed. Picking 14 August on the calendar today records that due time and
  puts the message at the head of the queue, so it runs immediately.

### The queue popover

On app open, **one** popover listing everything queued — not one per message —
with **cancel each** and **cancel all**.

No concurrency cap: a running task carries its own cancel button, which covers
the burst case.

---

## 10. Sidebar and filters

* **Inbox loses its sidebar link.** Its job — triage, live pulse, the list of
  every session — is now what the Tasks page does. The route itself stays
  reachable by direct navigation, so nothing already open breaks; it is simply
  no longer advertised.
* Filters: **status** (existing) plus **project**, auto-detected from the set of
  folders that have tasks.

---

## 11. What this needs built

### New stores — both global, not branch-scoped

Sessions are global, so anything keyed by session must be too. This follows the
existing precedent: `claude_sessions.py` deliberately skips the branch nesting
that `storage.home_dir()` applies, so triage and names are shared across
worktrees.

| file | contents |
| --- | --- |
| `read.json` | session → last-read marker, for unread counts |
| `task_ids.json` | session → (project, n), allocate-once task numbering |

`task_ids.json` needs a **one-time backfill** over the existing sessions, in
creation order per project.

### New endpoints

* **last-N messages per task** — feeds the List accordion for every task at
  once. A tail parse, cached the way the summaries endpoint already caches its
  head parse.
* **full message list for one task** — feeds Show more. Separate, because a
  full transcript parse is too expensive to do for every row.

### Schedule changes

* remove the past-due creation guard (`schedule.py:535`),
* make one-off catch-up unbounded,
* replace the recurring 120-second bound with coalesce-to-latest,
* the queue popover and its cancel actions.

### Migration

* Existing scheduled entries become messages on tasks; those already carrying a
  `claude_session_id` attach to that task, the rest become pending tasks (§5).
* Existing sessions get task IDs from the backfill.
* No transcript is ever deleted or rewritten. Archiving a task is a triage flag,
  nothing more.

**No catch-up migration is needed, and this is worth stating because it looks
like it should be.** Removing the bound cannot resurrect old work: the sweep
writes `MISSED` to the store and persists it, and it only ever acts on entries
that are still `PENDING`. Anything that outlived the old bound is already
terminal. The only entries the unbounded rule newly reaches are ones that stayed
pending past a day — which requires the app to have not run at all — so on a
store that has been in use, first boot after this change fires exactly what it
would have fired before.

---

## 12. Decided, but out of scope for this pass

* Auto-filling `description` from a chat summary — Claude Code stores no
  summary, so this needs an LLM call. Deferred.
* A per-day chip cap on the calendar.
* Confirm/undo on archiving a task.
* The DST spring-forward duplicate ghost chip (cosmetic; firing is already
  safe).

## 13. Edge cases with settled answers

* **A task whose last message is cancelled** — the task is deleted. There is no
  empty-shell state.
* **A task with a queued future message and completed past ones** — status
  follows the newest message, so it reads Upcoming.
* **Two messages queued into the same task** — they serialise, because
  `_busy_sessions` already holds the second back until the first turn ends.
