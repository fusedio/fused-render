# Specialists v2: the plan for the rebuild

Status: a plan. Nothing in this file is built. `docs/specialists.md` describes
version 1 as it exists on this branch; this file says what the rebuild changes.
Version 1 was judged over-engineered and too complex to use (review of
2026-11-19). The direction below came out of that review.

Review pages (private artifacts, owner's account):

- UX v2 proposal, screen by screen with mock-ups: https://claude.ai/artifact/HQK3nifhM4YuH3AtUYicxd
- Overview: the user's journey, then the engine: https://claude.ai/artifact/Cs3g9dyEWS3bh4Dnejwkcr
- V1 walkthrough (screenshots and videos): https://claude.ai/artifact/VgCPvJnDveSZQcbeYBZJpo

## Decision, 2026-11-19: a specialist is a kind of bot, with its own engine

- Specialists live on the **Bots page** as a separate kind of bot called
  **Specialist** (badge SPECIALIST), next to Super Bot and Expert bots. There
  is no Specialists page and no sidebar entry. Reason: having both Bots and
  Specialists in the sidebar was confusing.
- The specialist **keeps its own engine**, as planned in this file (folder as
  a Claude Code project, task chat engine, three task tools, inbox, daily or
  manual update, one commit per update). It is **not** folded into the Expert
  kind. Reasons given by the owner: the engine is different and should stay
  so; Expert and Specialist solve nearly the same problem and the owner wants
  to compare the two approaches.
- Expert bots (`docs/experts.md`, `fused_render/experts/`) are left as they
  are.

What this changes in the plan below: only where the specialist appears.
"Specialists page" now means its row and chat on the Bots page. The Memory
pane, creation, task side, breakout and update are as written.

Consequences to design for:

- **Two chat engines on one page.** A specialist's chat is the task chat
  (tool rows, approval cards, model pill), not the Bots bubbles. This is the
  one place the different engine shows; accepted.
- **The list merges two sources.** Preferred: the Bots list also lists
  specialists from the specialists API and renders them as rows (face, name,
  badge, last message, time, new dot), rather than registering specialists in
  the bots backend. To be confirmed against the Bots code before building.
  Shared row actions (pin, hide, mark as read, search) must work or be left
  out for specialist rows on purpose.
- **Create:** the Bots + flow gains a Specialist choice (name, what it is an
  expert on, optional folder).
- **Badge:** neutral fill (`--row-bg-selected`), so no new colour.
- **Tasks see both tool sets** (`expert_*` and the specialist tools) when
  both kinds exist. On one project, run one of the two at a time, or a task
  is handed guidance from both.
- **Switch off:** no specialist rows, no Specialist choice under +, no
  tools, no "With" choice.

Comparing Expert and Specialist (the owner's stated goal):

| | Expert | Specialist |
|---|---|---|
| On disk | Bot folder: instructions, one memory file, skill files | Claude Code project: description, memory, skills, inbox, git history |
| Own chat | Bots chat engine, fixed write limits | Task chat engine, usual permission modes |
| How a task uses it | Reads its memory, or asks one question | Gets guidance (automatic in its folder), or opens a breakout |
| User in the loop | No | Yes, in the breakout card |
| Learning from tasks | Reads a task minutes after it consulted | Notes; the update reads notes, tasks, breakouts |
| Takes effect | Minutes later | Next update (daily, or Update now) |
| Task that never consulted it | Teaches nothing | Can send a note |
| Memory format | Fact per line with count and date | Plain sentences |
| Trace | Changes: row per session with diff | Changes: row per commit with diff, plus a chat message per update |
| Undo | Edit the memory file | Reply in chat, or edit the file |
| Messaging people | No | Connected services, with approval |

What to watch: whether tasks follow the rules more often; how soon a taught
rule shows up in the next task; how clean memory is after a week; effort to
fix a wrong entry; model runs spent. A fair test gives both the same subject
and project (the shipped Canvas expert and a Canvas specialist).

## What is decided and what is not

Decided by the owner:

- V1's UX is too complex and must be cut down. Remove any feature that does not
  give real value.
- No new colours. Use the app's existing tokens and components.
- The memory update ("dream") runs once a day, or by hand. Running it more
  often is a possible follow-up and is out of scope.
- The update's trace is one commit, one chat message and the Changes list (see
  "The update and its history"). It must stay very simple.

Proposed and awaiting the owner's go-ahead: everything else in this file.

Settled since: specialists are a kind of bot on the Bots page with their own
engine (see the decision above). Nothing is open at design level.

## Rules for the rebuild

1. Reuse before building. The Bots page already has a Memory pane (Memory,
   Edit, Changes) and a `≡` menu. The plain breakout already has pick mode,
   the floating card, Send back and Close. The Claude chat already has tool
   rows and an approval card. V2 uses these as they are.
2. No specialist-only colours. Delete `--spx-tint`, `--spx-draft`, `--spx-warn`,
   `--spx-card-tint`. Use `--bg`, `--bg-alt`, `--row-bg-hover`, `--fg`,
   `--fg-muted`, `--border`, `--accent`, and the chat's `--c-accent`.
3. Never show machinery: no entry ids, note kinds, note states, item states,
   provenance fields, commit hashes.
4. The specialist's chat is the one place it tells the user anything.

## The three ideas

1. A specialist is a chat that remembers. It is a kind of bot on the Bots page.
2. Tasks use it by themselves. The user sees an ordinary tool row.
3. Breakout puts it beside the task, through the existing breakout flow.

## Screens

### A specialist on the Bots page

- A row in the Bots list: face, name, SPECIALIST badge, last message, time, a
  dot when there is something new. Selecting it shows its chat and a right
  pane that is closed until opened.
- Header: Clear, and the pane toggle. Clear folds the earlier chat into one
  line with Show, as approved earlier.
- No side panel. No settings dialog with tabs.
- Faces come from the Bots face picker.

### Memory pane (replaces Settings)

- Tabs: Memory, Skills. Memory is plain sentences with Edit (direct text edit).
- Who owns what is a sentence in memory, not a People table.
- `▸ Notes waiting · n`: open to read or delete waiting notes.
- `▸ Changes · n`: the history list (below).
- `≡` menu: Settings… (name, one-line description, optional folder), Update
  now, Delete….
- Connected services are the app's own. Every send asks with the app's
  approval card. No per-specialist allow-list.

### Creating one

Two fields: name, and what it is an expert on. One optional field: a folder it
helps in by itself. The model is chosen in the message box like any chat.

### Inside a task

- "Used the <name> specialist" and "Sent a note to the <name> specialist" are
  ordinary collapsed tool rows. Opening one shows what was given or sent; Undo
  for a note is inside.
- No "Use now". A note waits for the next update.
- No "Send to a specialist" button, no picker, no general inbox. A note always
  names a specialist.

### Breakout

- Start: the existing flow (More options, Breakout…, pick exchanges, type the
  question, Start breakout). The pick bar gains one control, "With", to choose
  a specialist or leave it plain. No second Breakout button, no brief form.
- When the task offers: the standard approval card, one sentence, Open and
  Not now. The task decides when to offer; there is no stuck detector.
- Card: the plain breakout card. Where it says "research only" it names the
  specialist. Same "n messages from…" seed row. Same Send back and Close.
- Send back writes a handoff note into the task's message box as text, unsent.
  Close asks "Discard this breakout?" and leaves nothing in the task. No
  closed chip, no Reopen.
- The specialist breakout keeps the same privileges as the main chat.
- The plain breakout is itself a beta switch, so Specialists on implies it on.

## The journey

1. Make one: name, what it is an expert on, optional folder.
2. Teach it by talking in its chat; it writes memory at once.
3. Start a task in its folder; it helps by itself ("Used the … specialist").
4. A task, or the user through a task, sends it a note ("Sent a note to …").
5. When stuck, start a breakout "With" the specialist, or approve the task's
   offer. The task keeps working.
6. Talk it through in the card. If the problem is someone else's, it offers
   to tell them; every send needs approval.
7. Send back puts a handoff note in the task's message box.
8. Once a day or on Update now, it folds what it gathered into memory and
   says what it did in its chat. Then back to step 3.

## The engine

- **A folder, not a database.** `CLAUDE.md` (who it is, pulls in memory),
  `memory.md`, skills, `inbox/`, and git history. An ordinary Claude Code
  project.
- **Conversations are normal Claude Code sessions in that folder**, on the
  same chat engine as tasks. The main chat reads and writes memory directly.
- **Tasks get three tools:** get guidance (memory plus matching skills; fetched
  automatically in the specialist's folder), send a note (a file in `inbox/`),
  open a breakout (raises the approval card).
- **A breakout is the plain breakout pointed at the folder.** It has the main
  chat's abilities but does not edit memory itself. There is no close-out
  note written by the model.
- **The inbox is the one place learning waits, and it fills as things
  happen.** Everything a specialist will learn from is a file in `inbox/`,
  written by the app at the time: a note when a task sends one; a breakout's
  conversation as each of its turns ends; a task's conversation, from the
  point it first used the specialist, as each of its turns ends. "Notes
  waiting" lists all three. (Decided 2026-11-19 after the owner's first test:
  a breakout that left nothing in the inbox read as "no learning".)
- **The update is one scheduled run** (next section). It never sends messages
  to people.
- **History is git.** The app commits after every chat turn that changed
  something, every manual edit and every update.
- **Messages to people** go through the app's connected services, with the
  app's approval card, only in a live chat or breakout.
- **Experimental switch, off by default.** Off means no page, no tools, no
  "With" choice; tasks behave as today.

## The update and its history

- Runs once a day while the app is open and idle, or from Update now.
- With an empty inbox there is no run, no commit and no message.
- It is one unattended turn in the specialist's own chat. It reads the inbox
  and nothing else, edits memory and skills, and the app then removes the
  inbox files that were there when it started, whether used or left out.
- **Record:** one git commit per run in the specialist's folder. The diff is
  the trace: notes leave `inbox/`, lines appear in memory or a skill. The
  commit message is a few plain sentences the update writes: what it added,
  what it changed, what it left out and why.
- **Notification:** the same sentences, posted once as a message in the
  specialist's chat. To undo something the user replies; an Undo link is only
  a shortcut that sends "Undo: <line>".
- **Looking back:** Changes in the Memory pane. An update is one row
  ("Update · date"), beside rows for the user's own edits and chat turns that
  changed memory. Opening a row shows its diff.
- A failed run commits nothing, keeps the notes, and posts one chat line.
- Removed from v1: the structured change list and its validator, reconciling
  it with the diff, item kinds and states, the note-to-change mapping, the
  "ignored" list, entry ids and per-entry metadata in memory, "confirmed
  again" counts, the "seen" marker, the Learned tab and Revert tracking.
- Given up: clicking one memory line to see its source note, and a "Reverted"
  mark on an undone change.

## Cut list

Removed: side panel; Learned tab; general inbox; "Send to a specialist";
People tab; Connections tab and per-specialist allow-list; overnight drafts to
owners; trigger words, effort and the charter editor; second Breakout button
and its menu; brief form; stuck detector; plan-check tool; after-task
reviewer; structured change list.

Simplified: memory entries (plain text); notes inbox (one line with a count);
guidance and note chips (tool rows); offer card (approval card); summary
(handoff note in the message box); Close and Discard (Send back and Close).

Kept: the chat that remembers, Clear and the folded earlier chat; guidance
reaching tasks by folder or by the task's judgement; notes from any task;
breakout beside a task with both working; update applied by default with
undo; messaging an owner through connected services with approval; the
experimental flag, off by default.

Tools handed to a task go from five to three: get guidance, send a note, open
a breakout.

## Known trade-offs

- A rule stated inside a task takes effect at the next update, not at once.
  Telling the specialist in its own chat, or Update now, covers urgent cases.
- No overnight reports to owners; the specialist offers during a live chat.
- The task's judgement replaces the stuck counter, so it may offer less often.
- No reopening a closed breakout from the task.
- Every send to a service asks.
