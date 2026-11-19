# Specialists: design record for the rebuild

The rebuild is done. How it works now, route by route, is `docs/specialists.md`; this page keeps what was decided
and why. Version 1 was judged over-engineered and too complex to use (review of 2026-11-19), and was rebuilt much
smaller from the direction below.

Review pages (private artifacts, owner's account):

- UX v2 proposal, screen by screen with mock-ups: https://claude.ai/artifact/HQK3nifhM4YuH3AtUYicxd
- Overview: the user's journey, then the engine: https://claude.ai/artifact/Cs3g9dyEWS3bh4Dnejwkcr
- V1 walkthrough (screenshots and videos): https://claude.ai/artifact/VgCPvJnDveSZQcbeYBZJpo

## Decided by the owner

- Version 1's UX was too complex and had to be cut down: remove any feature that does not give real value.
- No new colours. Use the app's existing tokens and components.
- The memory update runs once a day, or by hand. Running it more often is a possible follow-up, out of scope.
- The update's trace is one commit, one chat message and the Changes list, and it must stay that simple.

## Rules for the rebuild

1. Reuse before building: the Bots page's memory pane and `≡` menu, the plain breakout (pick mode, floating card,
   Send back, Close), the Claude chat's tool rows and approval card, as they are.
2. No specialist-only colours.
3. Never show machinery: no entry ids, note kinds or states, provenance fields or commit hashes.
4. The specialist's own chat is the one place it tells the user anything.

## Decision, 2026-11-19: a kind of bot, with its own engine

- A specialist lives on the Bots page as its own kind of bot (badge SPECIALIST), next to Super Bot and Expert. There
  is no Specialists page and no sidebar entry: having both Bots and Specialists in the sidebar was confusing.
- It keeps its own engine (a folder that is a Claude Code project, the task chat engine, three task tools, an
  inbox, a daily or manual update, one commit per update) and is not folded into the Expert kind. The owner's
  reasons: the engine is different and should stay so, and Expert and Specialist solve nearly the same problem, so
  the two approaches can be compared. Experts (`docs/experts.md`) are left as they are.
- Accepted consequences: two chat engines on one page (a specialist's chat is the task chat, not the Bots
  bubbles); the Bots list merges two sources, and row actions that do not fit a specialist are left out on purpose;
  tasks see both tool sets when both kinds exist, so on one project run one of the two at a time.
- What to watch in the comparison: whether tasks follow the rules more often; how soon a taught rule shows up in
  the next task; how clean memory is after a week; the effort to fix a wrong entry; model runs spent. A fair test
  gives both the same subject and project, which is why Canvas and Data ship as both an expert and a specialist.

## Decision, 2026-11-19: the inbox fills as things happen

Everything a specialist learns from is a file in its `inbox/`, written by the app at the time: a note when a task
sends one, a breakout's conversation as each of its turns ends, and the conversation of a task that used the
specialist as each of its turns ends. "Notes waiting" lists all three. Decided after the owner's first test: a
breakout that left nothing in the inbox read as "no learning".

## Cut list

Removed from version 1: the side panel; the Learned tab; the general inbox and "Send to a specialist"; the People
tab; the Connections tab and the per-specialist allow-list; overnight drafts to owners; trigger words, effort and
the charter editor; the second Breakout button and its menu; the brief form; the stuck detector; the plan-check
tool; the after-task reviewer; the structured change list with its validator, item kinds and states, the
note-to-change mapping, the "ignored" list, entry ids and per-entry metadata, "confirmed again" counts, the "seen"
marker and Revert tracking.

Simplified: memory entries became plain text; the notes inbox one line with a count; guidance and note chips
ordinary tool rows; the offer card the standard approval card; the summary a handoff note in the message box;
Close and Discard became Send back and Close. Tools handed to a task went from five to three.

Kept: the chat that remembers, with Clear and the folded earlier chat; guidance reaching tasks by folder or by the
task's own judgement; notes from any task; a breakout beside a task with both working; the update applied by
default; messaging an owner through connected services with approval; the experimental switch, off by default.

## Known trade-offs

- A rule stated inside a task takes effect at the next update, not at once. Telling the specialist in its own
  chat, or Update now, covers urgent cases.
- No overnight reports to owners; the specialist offers to message someone during a live chat.
- The task's judgement replaces the stuck counter, so it may offer a breakout less often.
- No reopening a closed breakout from the task.
- Every send to a connected service asks.
- Given up: clicking one memory line to see the note it came from, and a "Reverted" mark on an undone change.
