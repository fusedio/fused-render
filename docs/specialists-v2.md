# Specialists v2: the plan for the rebuild

Status: a plan. Nothing in this file is built. `docs/specialists.md` describes
version 1 as it exists on this branch; this file says what the rebuild changes.
Version 1 was judged over-engineered and too complex to use (review of
2026-11-19). The direction below came out of that review.

Review pages (private artifacts, owner's account):

- UX v2 proposal, screen by screen with mock-ups: https://claude.ai/artifact/HQK3nifhM4YuH3AtUYicxd
- Overview: the user's journey, then the engine: https://claude.ai/artifact/Cs3g9dyEWS3bh4Dnejwkcr
- V1 walkthrough (screenshots and videos): https://claude.ai/artifact/VgCPvJnDveSZQcbeYBZJpo

## Direction change, 2026-11-19: merge into Bots

The owner leans towards putting specialists on the Bots page rather than a
separate page, because having both Bots and Specialists in the sidebar is
confusing. Not yet final; three questions are open (below).

Finding that drives this: the Bots page already has **Expert bots**
(`docs/experts.md`, `fused_render/experts/`, about 1,900 lines). An expert
already is a chat that remembers one subject: `memory.md`, skills, three task
tools (`expert_list`, `expert_context`, `expert_ask`), learning from the tasks
that used it a few minutes after they call it, memory compaction, a Memory
pane with Edit and a Changes list with a diff per session.

Proposed shape of the merge: do not add a third kind. Extend the Expert kind
with what it lacks, behind the experimental switch:

1. Breakout "With" an expert, beside a task (the plain breakout card pointed
   at the expert's folder, instructions and memory).
2. A note tool, so any task, or the user through a task, can tell an expert
   something even if the task never consulted it.
3. Automatic help in a folder the expert is assigned to.
4. Messaging an owner through connected services, with approval, from the
   expert's attended chat or breakout. Do this last.

Consequences for the rest of this file:

- "Specialists page", "Memory pane" and "Creating one" are already built as
  the Bots page, the expert's pane and its Settings. Nothing to build there.
- The inbox plus nightly update would not be built. The expert's existing
  after-task learning stays, and notes and breakout conversations feed it.
- The update trace is the existing Changes list (one row per session that
  changed memory, with its diff), not git commits. A chat message per update
  is probably not wanted when learning happens several times a day.
- Memory keeps the expert format: one fact per line with a count and a date,
  under "Works" and "Does not work".
- Most of the v1 specialists code on this branch is not carried over. Worth
  porting: launching a breakout session into a specialist folder, the note
  tool, the folder hook.
- The earlier instruction "do not touch other systems" no longer holds: this
  changes Bots and experts code. New abilities stay behind the switch.

Open questions put to the owner: the name (Expert or Specialist); whether to
keep the expert's after-task learning in place of the inbox and nightly
update; whether an update should still post a chat message.

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

Still open: whether Specialists stays a separate page or becomes a kind of bot
in the Bots list. Working assumption: separate page while experimental, as
originally instructed; nothing in Bots is touched.

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

1. A specialist is a chat that remembers. Its page copies the Bots layout.
2. Tasks use it by themselves. The user sees an ordinary tool row.
3. Breakout puts it beside the task, through the existing breakout flow.

## Screens

### Specialists page

- Layout as Bots: list (face, name, last message, time, a dot when there is
  something new), chat, and a right pane that is closed until opened.
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
  note: the update reads the breakout's conversation later. (Proposed; this
  replaces v1's marked summary-and-note block.)
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
- With no notes there is no run, no commit and no message.
- It is one unattended run in the specialist's folder. It reads the waiting
  notes, the tasks that used the specialist and its breakouts, edits memory and skills, and
  removes the notes it read, whether used or left out.
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
