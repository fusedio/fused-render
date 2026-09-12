// THE PROJECT QUEUE'S WORDS, in one place — "1st in line · behind TASK-038".
//
// Three surfaces say it: the Tasks List row, the Tasks Board card and the native
// chat (its waiting rows and the card over the composer). Two of those are shell
// and one is an app, and an app may not import shell
// (scripts/check-boundaries.mjs) — so the builder lives here, in the layer both
// may read, rather than as two copies that describe one task's place in two
// wordings.
//
// PURE, AND NOTHING BUT THE WORDS (and one href). It takes the fields the server
// already decided (`Task.queue_*`, routers/tasks.py) and returns strings. It
// does not know what a Task is, it never asks whether a folder is busy — that is
// a fact about live processes and the server's alone — and it has no opinion
// about which ink any surface spends on it.
//
// THE VOCABULARY CHANGED ON 2026-09-12 (Akshil), and the old one is worth
// naming because every one of these strings replaces one:
//
//   * `#2 in line` → `2nd in line`. A `#` is a database row number; an ordinal
//     is how a person says their place in a queue out loud.
//   * `behind TASK-041 "Pull today's news"` → `behind TASK-038`, a LINK. The
//     quoted title was a second sentence inside the first one, and it pushed the
//     one actionable token — the id — off the end of a narrow row. The title is
//     still there, on the pointer; the id is now something to press.
//   * `behind a run in this folder` → nothing at all. "Behind" with no name to
//     give was a half-sentence the reader could do nothing with; the honest
//     shape of "this is waiting and nothing else holds the folder" is to say
//     only that it is waiting.
//   * `Skip the queue` → `Run next`. Skip read as "skip this message"; the whole
//     point of the verb is that the message RUNS, next, and that nothing in
//     flight is interrupted.
//   * `queued` stays the STATUS WORD in code and on a row; `waiting` is the word
//     a count is said in ("2 waiting"), because a person reading a sidebar wants
//     the state described, not the enum named.

/** The queue facts a caption is built from — the subset of a task row that says
 *  where it stands, so a caller holding an admission answer rather than a row
 *  (the chat, whose message has no `/api/tasks` row yet) can ask the same
 *  question of it. */
export interface QueueFacts {
  /** The row's status word. Anything but `"queued"` has no place in a line. */
  status?: string;
  /** 1-based place in the folder's line. 0 or absent is an honest answer — an
   *  older server, or a row the server could not place yet — and it is NOT the
   *  head: it is "somewhere in the line", printed without an ordinal. */
  queue_position?: number;
  /** The holder's task id ("TASK-038"), or "" when the server could not name
   *  what is in front — which is also what a FREE folder answers. Empty means
   *  the caption says nothing about being behind anything: see `queueBehind`. */
  queue_ahead?: string;
  /** …and that holder's title. NEVER ink any more — it is the pointer's text
   *  and nothing else. A row's one actionable token is the id. */
  queue_ahead_title?: string;
  /** The holder's Claude session and its folder — the two halves of the link the
   *  id is drawn as (`queueAheadHref`). Absent on an older server, and then the
   *  id is plain text rather than a dead link. */
  queue_ahead_session?: string;
  queue_ahead_target?: string;
  /** The holder's own TASK KEY, when the server could name one. A run that has
   *  not published a session yet is keyed `pending:<entry id>` — which is a door
   *  after all (`chatUrl`'s `queued` param opens that entry's chat), so the id
   *  stays a link where it used to go plain. "" / absent on an older server. */
  queue_ahead_key?: string;
  /** Skipped (or holding a held answer): this one goes out next, and has the
   *  claim on the spot to prove it. THE ONLY thing that reads as the head — see
   *  `queueRunsNext`. */
  queue_priority?: boolean;
}

export interface QueueCaption {
  /** "1st in line", or a bare "in line" when the server could not place it. */
  place: string;
  /** "behind TASK-038", or "" when nothing has a name to give. */
  behind: string;
  /** The two, joined — what the ink actually says. */
  text: string;
  /** The holder's task id, for the link. "" when there is none. */
  ahead: string;
  /** The holder's title, for a pointer. "" when the server named none. */
  aheadTitle: string;
  /** Where the id points, or null when the holder has no session to open. */
  aheadHref: string | null;
  /** Whether this is the one that goes out next — `queue_priority` alone. The
   *  ⤒ glyph's condition, and the condition a surface kills Run next on. */
  runsNext: boolean;
}

/** Where in its folder's line, 1-based, 0 for "the server said nothing". */
export function queuePosition(facts: QueueFacts): number {
  const at = facts.queue_position ?? 0;
  return Number.isFinite(at) && at > 0 ? Math.floor(at) : 0;
}

/**
 * Does this go out the moment its folder frees?
 *
 * `queue_priority` AND NOTHING ELSE — never inferred from a position. Standing
 * at 1st looks like the same sentence, and it is not one: a position is where
 * this stood when the server last looked, and anything else in the folder can be
 * skipped over it in the next second. Only the flag is a CLAIM on the spot.
 * Reading 1st as the head told the reader "runs next" about a place they might
 * lose — and, worse, took away the one control that would have made it true, by
 * killing Run next on the row that most wanted to press it (browser QA,
 * 2026-09-12).
 */
export function queueRunsNext(facts: QueueFacts): boolean {
  return facts.queue_priority === true;
}

/**
 * "1st", "2nd", "3rd", "12th" — a place said the way a person says it.
 *
 * The teens are the whole reason this is a function and not a suffix table
 * lookup: 11, 12 and 13 take "th" while 21, 22 and 23 do not, and a queue twelve
 * deep in a busy folder is not a hypothetical. 0 and anything unreadable answer
 * "" so the caller can fall back to the placeless wording rather than printing
 * "0th".
 */
export function queueOrdinal(n: number): string {
  if (!Number.isFinite(n) || n < 1) return "";
  const i = Math.floor(n);
  const tens = i % 100;
  if (tens >= 11 && tens <= 13) return `${i}th`;
  switch (i % 10) {
    case 1:
      return `${i}st`;
    case 2:
      return `${i}nd`;
    case 3:
      return `${i}rd`;
    default:
      return `${i}th`;
  }
}

/**
 * "behind TASK-038" — ONLY when a DIFFERENT task is holding the folder.
 *
 * An empty `queue_ahead` is a real answer and it gets NO words. It used to read
 * "behind a run in this folder", which is a sentence with a hole in it: there is
 * nothing to look at, nothing to press and nothing to do about it, and a reader
 * who has just typed into their own chat is being told about a stranger that may
 * not exist. The folder can be perfectly free — a second message waits behind
 * the first one this chat sent, which is the conversation keeping its own order
 * — and the honest rendering of that is that it is waiting, full stop.
 */
export function queueBehind(facts: QueueFacts): string {
  const ahead = (facts.queue_ahead || "").trim();
  return ahead ? `behind ${ahead}` : "";
}

/**
 * WHERE THE ID POINTS: the holder's own conversation.
 *
 * "behind TASK-038" is only worth printing if TASK-038 is somewhere the reader
 * can go — the one question a person has about the thing in their way is what it
 * is doing. The server names the holder's session and folder beside its id
 * (`queue_ahead_session` / `queue_ahead_target`); with either missing the id
 * stays plain text, which is what an older server produces and is a strictly
 * better answer than a link to nothing.
 *
 * THE SAME CODEC THE SHELL'S `explorerUrl` SPENDS — and the shell's now
 * delegates here, so the app layer and the shell cannot disagree about where a
 * conversation lives. It is written out rather than imported from
 * `platform/lib/router`, which touches `location` and `history` at module init:
 * this module is pure, and every one of its callers' tests depends on that.
 */
export function queueAheadHref(facts: QueueFacts): string | null {
  const session = (facts.queue_ahead_session || "").trim();
  const target = (facts.queue_ahead_target || "").trim();
  if (!target) return null;
  if (session) return chatUrl(target, session);
  // A HOLDER THAT IS STILL STARTING is not a dead end any more. Its row is keyed
  // `pending:<entry id>` until its run opens a session, and that entry IS a
  // conversation the reader can open (`QUEUED_PARAM`) — so the id keeps its
  // underline through the one window it used to lose it in.
  const entry = pendingEntryId(facts.queue_ahead_key || "");
  return entry ? chatUrl(target, "", entry) : null;
}

/** THE KEY PREFIX A TASK WEARS BEFORE IT HAS RUN — `pending:<entry id>`, the
 *  server's own name for a task that is nothing but a line in a folder's queue
 *  (routers/tasks.py). One spelling, because three surfaces take it apart. */
export const PENDING_KEY_PREFIX = "pending:";

/** The entry id inside a `pending:<id>` key, or "" for every other key. */
export function pendingEntryId(key: string | null | undefined): string {
  const k = (key || "").trim();
  return k.startsWith(PENDING_KEY_PREFIX) ? k.slice(PENDING_KEY_PREFIX.length) : "";
}

/**
 * THE URL PARAM THAT OPENS A CHAT WHICH HAS NEVER RUN — `queued=<entry id>`.
 *
 * A waiting NEW chat has no session: nothing of its has run, so there is no
 * transcript, no `session_id` and nothing for `chatUrl`'s ordinary shape to
 * name. What it DOES have is the entry its first message is, and the server
 * groups the whole conversation under it (`pending:<leader id>`). So the leader
 * id is the chat's name until the scheduler gives it a real one, and a pane that
 * mounts with this param remembers it as its queue leader — which is what draws
 * the waiting rows, reads the right `/api/tasks` row for the header, and adopts
 * the session the moment the leader runs.
 */
export const QUEUED_PARAM = "queued";

/** The codec itself, so `shell/schedule-lib.explorerUrl` has one to delegate to
 *  instead of keeping a second copy of the same three lines.
 *
 *  `queuedEntryId` is the third, optional half: a chat with no session yet is
 *  opened by the entry it is waiting as. Never both — a session outranks it, and
 *  a chat that has one has nothing left to open by entry. */
export function chatUrl(target: string, sessionId: string, queuedEntryId = ""): string {
  const norm = /^[A-Za-z]:[\\/]/.test(target) ? target.replace(/\\/g, "/") : target;
  const encoded = norm
    .replace(/^\/+/, "")
    .split("/")
    .filter(Boolean)
    .map(encodeURIComponent)
    .join("/");
  const queued =
    !sessionId && queuedEntryId
      ? `&${QUEUED_PARAM}=${encodeURIComponent(queuedEntryId)}`
      : "";
  return `/explorer/view/${encoded}?_side=claude&session_id=${encodeURIComponent(sessionId)}${queued}`;
}

/**
 * The caption a Tasks row or Board card wears, or null when this is not queued.
 *
 * "1st in line · behind TASK-038", and a bare "1st in line" when nothing else
 * holds the folder. The status test is a plain `!== "queued"` rather than the
 * shell's `statusColumn` narrowing, which is not importable from here. The two
 * agree on the only value this asks about: an unknown status is not `"queued"`
 * either way.
 */
export function queueCaption(facts: QueueFacts): QueueCaption | null {
  if (facts.status !== "queued") return null;
  const at = queuePosition(facts);
  const ord = queueOrdinal(at);
  const place = ord ? `${ord} in line` : "in line";
  const behind = queueBehind(facts);
  return {
    place,
    behind,
    text: behind ? `${place} · ${behind}` : place,
    ahead: (facts.queue_ahead || "").trim(),
    aheadTitle: (facts.queue_ahead_title || "").trim(),
    aheadHref: queueAheadHref(facts),
    runsNext: queueRunsNext(facts),
  };
}

/**
 * "1 message waiting" / "2 messages waiting" — the card over the chat composer.
 *
 * The one place in this vocabulary that forks on plural, because it is the one
 * place the noun is spoken: "2 waiting" below needs no noun and therefore no
 * fork, while "2 messages waiting" would read as a typo without one.
 */
export function waitingCount(n: number): string {
  return n === 1 ? "1 message waiting" : `${n} messages waiting`;
}

/** "2 waiting" — the sidebar's and the lane header's readout, worded like the
 *  "2 running" it sits beside. No noun and no plural fork: one word names one
 *  state on every surface that says it. */
export function waitingLabel(n: number): string {
  return `${n} waiting`;
}

/** "1 running · 2 waiting" — the In Progress lane header, where the two groups
 *  the lane now holds are counted separately. Either half alone when the other
 *  is empty, so a lane with nothing waiting reads exactly as it always did. */
export function runningWaitingLabel(running: number, waiting: number): string {
  const parts: string[] = [];
  if (running > 0) parts.push(`${running} running`);
  if (waiting > 0) parts.push(waitingLabel(waiting));
  return parts.join(" · ");
}

/**
 * WHAT IS IN FRONT, FOR THE CARD OVER THE COMPOSER — "behind TASK-038", or
 * "next in this folder" once nothing is.
 *
 * The second half is not a consolation wording: it is the state a press of Run
 * next PRODUCES, and it is also the state a chat whose folder was never busy is
 * in from the start. One sentence for one fact, whichever road reached it.
 */
export const NEXT_IN_FOLDER = "next in this folder";

export function waitingCardText(count: number, facts: QueueFacts): string {
  const behind = queueRunsNext(facts) ? "" : queueBehind(facts);
  return `${waitingCount(count)} · ${behind || NEXT_IN_FOLDER}`;
}

/**
 * Is there anything for Run next to DO? The button's whole condition, written
 * once so the card, the row and the board cannot disagree.
 *
 * THE TEST IS THE POSITION, NOT "is something in front" (Akshil, 2026-09-12).
 * Every queued task has something in front of it — that is what queued MEANS —
 * and the thing in front is usually the RUN HOLDING THE FOLDER, which Run next
 * cannot touch: it interrupts nothing, by design. So at position 1 the press had
 * exactly one possible outcome, the state the reader was already in, while the
 * caption went on naming a task the control could not get in front of.
 *
 * `queue_position > 1` is "another WAITING task is ahead of me", which is the
 * only arrangement this verb can change. Position 0 — the server placed nothing
 * — is not a claim that anything is ahead, so it offers no button either.
 *
 * THE CAPTION IS NOT GATED ON THIS. `1 message waiting · behind TASK-056` is
 * true at position 1 and stays printed; only the button goes.
 */
export function canRunNext(facts: QueueFacts): boolean {
  return !queueRunsNext(facts) && queuePosition(facts) > 1;
}

/**
 * THE VERB, in the words every surface says it in.
 *
 * "Skip the queue" was the first wording and it was wrong in the one direction
 * that matters: it reads as skipping the MESSAGE. What the press does is send
 * this work to the front of its folder's line so that it is the next thing that
 * runs — and, critically, it interrupts nothing, which is the sentence the hint
 * exists to say out loud.
 */
export const RUN_NEXT_LABEL = "Run next";
export const RUN_NEXT_HINT = "Run next — nothing is interrupted";
export const RUN_NEXT_DONE_HINT = "Already next in this folder";

/**
 * The mark a row or card wears when its work is the next out of its folder.
 *
 * AN ARROW TO A BAR, and not a star or a bolt. It means "to the top of this",
 * which is exactly what Run next does and is the only thing it does: the run
 * holding the folder keeps running. A lightning glyph would promise the one
 * thing this feature must never be read as offering.
 */
export const QUEUE_PRIORITY_GLYPH = "⤒";
