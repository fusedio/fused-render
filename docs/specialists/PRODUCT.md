# Specialists — product spec

Status: agreed design, 2026-11-18. This page is the contract the implementation is checked against. It says what
the product does and what the user sees. It does not say how the code is laid out; `PLAN.md` beside it does.
When the two disagree about behaviour, this page wins.

Design references on this machine (read them, they are the pictures behind this text):

- Design page: `/tmp/claude-0/-home-user-fused-render/740fc3e5-7ab2-5807-a131-ab3d2c86eb7f/scratchpad/probot-specialist-system.html`
- Screen mock-ups (one HTML file per screen): `/tmp/claude-0/-home-user-fused-render/740fc3e5-7ab2-5807-a131-ab3d2c86eb7f/scratchpad/ui/project/*.dc.html`
  (`Main` = specialist page, `Learned` and `Memory` = settings tabs, `TaskOffer`, `TaskBreakout`, `TaskReturn` = inside a task)

## 0. Ground rules

1. **The name is "specialist" everywhere**: code, tools, routes, UI text, files. Never "probot", "expert" or "bot".
2. **A new system.** Do not delete, rename or change the behaviour of bots, experts (`fused_render/experts/`),
   the plain breakout, tasks or connect. Reuse their code by calling it or by small additive seams. With the flag
   off, the app behaves byte-for-byte as it does on `main` today.
3. **One experimental feature flag, `specialists`, off by default.** Off: no page, no nav entry, no tools in task
   sessions, no timers, no folders created. It can be turned on from the UI where the app's other experimental
   switches live, and by an environment variable for tests.
4. **Everything runs on Claude Code.** A specialist session is a `claude` session. The app makes no model call of
   its own and never parses model prose to decide something, except reading clearly marked blocks it asked for
   (a summary block, a digest block).
5. **Local and personal.** A specialist lives on this machine for this user. No sharing, no export, in this version.

## 1. What a specialist is

An expert on one subject (example: Canvas). It is a **home folder that is an ordinary Claude Code project**, not a
running process. Any Claude Code session started in that folder is the specialist.

The home holds:

| Part | What it is | Who writes it |
|---|---|---|
| Charter | What the subject is, how the specialist behaves, the rules below | User (Settings), shipped seed |
| Memory | One always-loaded file, small, hard size cap. One entry per line with provenance (basis, sources, last confirmed, retires-when) | The nightly job only. Exception: the user tells the specialist to change it in its own chat |
| Topic knowledge | Files opened on demand; memory points to them | Nightly job |
| Skills | Procedures in Claude Code's skill format | Nightly job, user |
| People | Area → owner → how to reach them (which connected service, which handle or channel) | User, nightly job |
| Inbox | One raw note per file, not yet trusted | Anything (append only) |
| Receipts | Which task pulled what, at which memory version, when | The app |
| Changes | What each nightly update changed, entry by entry, with enough to undo it | The app + nightly job |
| Drafts | Messages to people that wait for the user | Nightly job, breakouts |

Memory and skills must load natively when a session starts in the home (Claude Code's own project memory and
skills mechanisms), not by prompt injection, wherever that is possible.

The home keeps history of every change to charter, memory, knowledge, skills and people so any change can be undone.

A user can create a specialist (name, one-line subject, optional "when to bring me in" triggers: folders and
keywords). One specialist, **Canvas**, is seeded the first time the flag is on, from the shipped Canvas material the
repo already has (`fused_render/experts/shipped/canvas-reference/`), copied, never moved.

## 2. Session kinds

All are Claude Code sessions whose working directory is the specialist's home.

| Kind | Started by | Seen where | Privileges | Ends with |
|---|---|---|---|---|
| **Main chat** | The user, on the specialist's page | The one chat on that page | Full: reads, edits and runs anywhere on the machine through Claude Code's normal permission prompts, shown as approval cards. Connected services as the user allowed for this specialist | Nothing to hand back |
| **Breakout** | A task (offer card) or the user (Breakout menu in a task) | A floating card beside the task | **Same as main chat.** One addition: every *write* to a connected service raises a card, even for a service the user ticked "don't ask" | Close: a short summary drafted into the task's composer, and a full note in the inbox |
| **Reviewer** | The app, after a task that pulled guidance goes quiet | Nowhere (a log row) | Read-only, plus writing its one inbox note | One inbox note |
| **Check** | A task calling `specialist_check` | Nowhere (a log row) | Read-only | An answer returned to the task |
| **Nightly job** | The app, once a day when open and idle; also "Run now" | A log row and the Learned list | Writes inside the home only. Reads allowed connected services. Never writes to a connected service | Memory updated, a change list, drafts |

Rules for every kind except main chat and nightly: **never rewrite memory, knowledge, skills or people**. They may
only add inbox notes. This is enforced with Claude Code permission rules, not only by instruction.

**Main chat is one conversation per specialist.** `Clear` (button and `/clear`) starts a fresh session in the same
thread; the earlier one folds under a marker with a Show link. There is no list of conversations and no second
main chat. Breakouts are separate sessions and run in parallel with it and with each other.

## 3. Three ways to use a specialist

### 3.1 Start with the specialist (main chat)
The user opens the specialist's page and types. Memory and skills are in context from the first turn. It does the
work itself, including editing files in the user's projects after an approval card.

### 3.2 Pull (a task borrows guidance)
No session starts. The task receives: memory, the list of topic knowledge and skills with their paths (it can read
them), and any "you said so" notes not yet digested. Two triggers:

- **By choice**: the task calls `specialist_list`, then `specialist_pull`.
- **By signal**: when a task starts or receives a message in a folder, or with words, that match a specialist's
  triggers, the app adds the guidance without the task asking.

Either way a **receipt** is recorded, the pulled text ends with one line asking the task to send a note if the
guidance turns out wrong or incomplete, and the task's transcript shows a quiet chip: "Guidance from the Canvas
specialist was added · N entries · View".

`specialist_check(specialist, stage, summary)` with stage `plan` or `done`: a short read-only specialist session
reviews the task's plan or result and answers. The pulled text tells the task to call it once after planning and
once before declaring done.

### 3.3 Breakout (a task hands a problem to the specialist)
1. **Offer.** The task calls `specialist_breakout(specialist, problem, tried, look_at)`. That raises an approval
   card in the task chat titled "Hand this to the <name> specialist?" showing the brief, with **Open breakout**,
   **Edit brief**, **Keep trying**. The tool returns at once in every case. On approve it returns: handed off, do
   not work on this problem, continue with the rest.
2. **Stuck detection.** The app counts repeated failures in a task (same tool failing repeatedly) and, past a
   threshold, tells the task to offer a breakout. An agent rarely notices it is going in circles.
3. **By hand.** The task's Breakout control offers "A copy of this chat" (today's breakout, unchanged) and each
   specialist. Choosing a specialist opens the same card with a brief the user types or edits.
4. **The card.** Today's floating breakout card, reused. Header: specialist's name, "Breakout from <task>",
   **Discard** and **Close**. Body: an ordinary chat. The first item is the brief. The session is fresh (not a
   fork): specialist's home, memory loaded, and it is told where the task's transcript and folder are and may read
   them.
5. **In parallel.** The task keeps running. The specialist is told the task is live in that folder, so it prefers
   advising, lists any files it changed, and leaves commits and branches to the task. No lock.
6. **Reporting a product bug.** The specialist finds the owner in People, drafts the message and asks; the send
   goes through the connected-service tool and its approval card shows recipient, service and exact text.
7. **Close.** The specialist is asked for its close-out: a summary in a fixed short shape (Outcome, Found, Do
   next, Files changed, Full session) and a full inbox note. The summary lands as a **draft in the task's
   composer**, appended below anything already typed; the user edits and sends. The note is saved whether or not
   the draft is ever sent. A chip in the task says "Breakout closed · its full note is saved with the <name>
   specialist · Reopen".
8. **Discard.** No summary. The session's transcript stays and the nightly job still reads it.
9. A breakout cannot open a breakout. A task may have several open.

## 4. Learning: nothing is lost

Everything lands in the inbox as the same kind of note, then one nightly job digests it.

**A note**: `kind` (gap, correction, convention, suspected-bug, behaviour, case), `claim` (one sentence),
`evidence`, `basis` (user-said, verified, guess), plus what the app attaches: source (task or session id,
position in its transcript), date, app version, and for corrections the memory entry it concerns.

Sources:

| What happened | How it is captured | Needs someone to remember |
|---|---|---|
| Breakout closed | Close-out note | No |
| Breakout discarded or abandoned | Nightly job reads the session | No |
| Main chat | Nightly job reads the specialist's own sessions since last time | No |
| Task pulled guidance | Receipt → reviewer session → one note | No |
| Task touched the subject without asking | Pull by signal creates the receipt | No |
| User stopped, denied, interrupted, undid | App records these signals against the task; the reviewer gets them | No |
| User says "send this to the specialist" | Task calls `specialist_note`; also a "Send to a specialist" action on any message | User's choice |
| Task notices something | Task calls `specialist_note` | Bonus |
| No specialist fits | General inbox | No |

**Reviewer rules.** It runs when the task has been idle for a set time after a pull (configurable; short values for
tests). A task that pulled and then shows no errors, denials, interruptions or user corrections is **quiet**: it is
counted as a confirmation of what it pulled with **no model run**. Otherwise the reviewer reads only the transcript
from the pull onward, framed as data, and classifies: followed and worked (confirmation), followed and failed
(correction), solved something uncovered (gap), user corrected the task (convention), pulled and never used
(trigger was wrong).

**Sending a note** needs no approval card. The task shows a chip "Sent to the <name> specialist's inbox · Undo".

## 5. Using what was learned correctly

- **Held apart.** Nothing in the inbox reaches any session or any pull until the nightly job has digested it.
  Exception: a note with basis `user-said` is appended to what sessions and pulls load, at once, marked as such.
- **Notes are claims, never instructions.** Every prompt that carries note or transcript text frames it as data.
- **One writer** (see §2).
- **Provenance** on every memory entry; entries with basis `guess` are marked as unconfirmed.
- **Three failure kinds, three fixes** by the nightly job: did not know → add; was wrong → correct or retire;
  knew but the task never found it → fix how it is found (index, trigger, wording), not the knowledge.
- **Usage.** Confirmed and pulled entries stay in always-loaded memory; entries nobody uses move to topic knowledge.
- **Size cap** on always-loaded memory, enforced by telling the nightly job the size and the limit.

## 6. The nightly job

- Runs at most once a day per specialist, when the app is open and has been idle; a **Run now** control exists.
- Reads: inbox notes not yet digested, the specialist's own sessions since the last run (main chat, breakouts,
  including discarded ones), receipts and confirmation counts, open reports.
- **Applies its changes immediately. No approval step.** It may add, correct, retire and move memory entries,
  write topic knowledge, add or change skills, and update People.
- Promotion: note → memory entry → skill → automatic check → product fix. It takes each step itself when a
  problem keeps recurring. The last step needs other people, so it writes a **draft** and stops.
- Follows up open reports through allowed connected services (read-only) and retires entries whose bug is fixed.
- Leaves a **change list**: one item per change with kind (Learned, Corrected, Retired, Moved, New skill, New
  check, People), the text, where it came from, and what is needed to undo it.
- **Revert** on any item is a message to the specialist in its main chat asking it to undo that change; the item
  shows "Reverted" once that turn has finished. The specialist keeps the notes it came from so it does not
  re-learn it tomorrow.
- **Drafts to people are never sent by the job.** They wait under "Waiting for you" with **Review and send** and
  **Discard**. Review and send runs in the main chat and goes through the normal approval card.
- **No automatic test of memory changes.** (Replay testing is out of scope.)

## 7. Connected services

Specialists use the existing connect system (`fused_render/connect/`) in full: one global sign-in per service,
access granted per specialist, the token never reaches the model.

- Per specialist: a list of allowed services (Settings › Connections), same meaning as a bot's `connections`.
- Main chat: an allowed service's tools run without a card; a service not allowed asks every time, and the card
  offers "Approve, don't ask again", which allows the service for this specialist.
- Breakout: reads as main chat; **every write asks**, always.
- Reviewer and check: no connected services. Nightly job: read-only tools of allowed services.
- Local app tools (apps with an MCP manifest) are part of the same roster and follow the same rules.
- Not included: the bots' browser and the iMessage channel.

## 8. Screens

Match the mock-ups; the app's own components and tokens win over the mock-up's exact pixels.

**Specialists page** (nav entry "Specialists", only with the flag on). Laid out like the Bots page:
- Left: list of specialists (face, name, one line of status), a round "+" to create one.
- Middle: the one chat. Header: face (opens Settings), name, status line, **Clear**, **Search**, **Settings**.
  Folded earlier chats show as marker lines with Show. Approval cards appear inline. Composer accepts `/clear`.
- Right panel: **Learned last night** (latest items, each with Revert, "See all"), **Waiting for you** (drafts),
  **Inbox** (count, View), and at the bottom a quiet **In your tasks** block ("Helping in N tasks now" linking to
  the task, "Gave guidance to N tasks this week"). Breakouts are not listed or openable here.

**Settings dialog** (one shell, tabs): **Learned** (every change of recent updates with Revert, the "Waiting for
you" drafts, Run now), **Memory** (always-loaded entries with provenance, topic knowledge, the inbox with each
note's kind, basis, source and "In effect now" for user-said), **Skills**, **People**, **Connections**,
**General** (name, subject, triggers, model, the counts in §9, delete).

**Inside a task** (only with the flag on): the pull chip; the offer card; the Breakout control's specialist
choices; the specialist breakout card; the closed chip with Reopen; the summary draft in the composer, labelled as
a draft from the specialist; "Send to a specialist" on a message with the sent chip and Undo.

## 9. Counts we keep

Per specialist, shown in Settings › General: breakouts opened, repeat breakouts (same problem again, as judged by
the nightly job), pulls, pulls confirmed, corrections received, notes received by source. They are for the user to
see whether it is working. Nothing acts on them.

## 10. Out of scope

Sharing or exporting a specialist; replay tests; the bots' browser and iMessage; non-Claude backends for specialist
sessions (tasks on other backends simply do not get the tools); merging specialists with bots or experts.

## 11. Acceptance scenarios

Each must work end to end on a running server with a real `claude`, and has to be covered by automated tests with
the harness stubbed.

A. Flag off: no nav entry, no routes answer, no tools in a task's tool list, no folders or timers created.
B. Flag on, first run: the Canvas specialist exists, with charter, seeded skills and empty memory.
C. Create a specialist; it appears in the list; its home is a valid Claude Code project; delete it.
D. Main chat: memory entry and a skill are visibly used; editing a file in another folder raises a card;
   Approve edits it, Deny does not.
E. Clear: a new session starts in the same thread; the earlier one folds and can be shown; nothing carries over
   except memory.
F. "Remember X" / "change how you do Y" in main chat edits memory or a skill at once and is in history.
G. Pull by choice from a task: list, pull, receipt recorded, chip shown, user-said note included, undigested
   other notes not included.
H. Pull by signal: a task in a matching folder gets guidance with no tool call; receipt recorded.
I. `specialist_check` at plan and at done returns advice; a log row exists.
J. Offer card from the task: approve → breakout opens seeded with the brief, the task continues; decline → the
   task continues and is told so; edit brief → the edited brief is what the specialist sees.
K. Stuck detection: repeated identical failures make the task offer a breakout.
L. Breakout by hand from the Breakout control.
M. Breakout runs while the task runs; both make progress; the user can chat in the card.
N. Breakout close: summary draft appended to the task's composer (with and without text already there), note in
   the inbox even if the draft is cleared; closed chip; Reopen works; a second close drafts a follow-up.
O. Breakout discard: no draft; the nightly job still learns from the session.
P. Two breakouts from one task with two specialists at once. A breakout cannot open a breakout.
Q. Breakout tries to rewrite memory: refused by a permission rule; it can add an inbox note.
R. `specialist_note` from a task that never pulled; to a named specialist; with no fitting specialist (general
   inbox); "Send to a specialist" from a message; Undo removes the note.
S. Reviewer: a quiet task yields a confirmation and no model run; a task with a failure after the pull yields one
   note; a task interrupted by the user yields a note that carries the signal.
T. Nightly job (Run now): digests notes into memory with provenance; user-said beats guess; a correction changes
   the named entry; a duplicate raises priority instead of adding a line; the change list is written; memory stays
   under its cap.
U. Revert a change: the main chat shows the request and the answer; the item reads Reverted; memory is back.
V. A draft to a person waits; Review and send raises the card; Discard removes it.
W. Connected service in main chat: not allowed → card; "don't ask again" → allowed; allowed → no card. In a
   breakout a write always raises a card.
X. Restart the server mid-breakout, mid-review and mid-nightly: nothing is lost or run twice.
Y. Two specialists; a task whose subject matches both.
Z. Deleting a specialist with an open breakout or pending receipts does not break the task.
