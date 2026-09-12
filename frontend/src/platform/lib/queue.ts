// THE PROJECT QUEUE'S WORDS, in one place — "#2 in line · behind TASK-041".
//
// Three surfaces say it: the Tasks List row, the Tasks Board card and the native
// chat's chip under a queued bubble. Two of those are shell and one is an app,
// and an app may not import shell (scripts/check-boundaries.mjs) — so the
// builder lives here, in the layer both may read, rather than as two copies that
// describe one task's place in two wordings.
//
// PURE, AND NOTHING BUT THE WORDS. It takes the four fields the server already
// decided (`Task.queue_*`, routers/tasks.py) and returns strings. It does not
// know what a Task is, it never asks whether a folder is busy — that is a fact
// about live processes and the server's alone — and it has no opinion about
// which ink any surface spends on it.

/** The queue facts a caption is built from — the subset of a task row that says
 *  where it stands, so a caller holding an admission answer rather than a row
 *  (the chat, whose message has no `/api/tasks` row yet) can ask the same
 *  question of it. */
export interface QueueFacts {
  /** The row's status word. Anything but `"queued"` has no place in a line. */
  status?: string;
  /** 1-based place in the folder's line. 0 or absent is an honest answer — an
   *  older server, or a row the server could not place yet — and it is NOT the
   *  head: it is "somewhere in the line", printed without a number. */
  queue_position?: number;
  /** The holder's task id ("TASK-041"), or "" when the server could not name
   *  what is in front. */
  queue_ahead?: string;
  /** WHAT IS IN FRONT IS THIS CHAT'S OWN EARLIER MESSAGE — a follow-up typed
   *  into a chat whose first message is still waiting in the line (admit's
   *  `follow_of`). The folder itself may be perfectly free; what this one is
   *  behind is the reader's own previous send, which is a different sentence
   *  from "behind TASK-041" and the only one that is true here.
   *
   *  ONLY EVER FROM AN ADMISSION ANSWER, never from a `/api/tasks` row: a row
   *  describes a TASK's place in its folder, and "whose message is in front" is
   *  a fact about one MESSAGE. So the Board card and the List row pass this
   *  field absent and read exactly as they did. */
  behind_own?: boolean;
  /** …and that holder's title, for a caption only. */
  queue_ahead_title?: string;
  /** Skipped (or holding a held answer): this one goes out next, and has the
   *  claim on the spot to prove it. THE ONLY thing that reads as the head — see
   *  `queueRunsNext`. */
  queue_priority?: boolean;
}

export interface QueueLine {
  /** "#2 in line", "runs next" at the head, or a bare "in line" when the server
   *  could not say where. */
  head: string;
  /** "behind TASK-041", or "behind a run in this folder". */
  behind: string;
  /** The two, joined — what the ink actually says. */
  text: string;
  /** Whether this is the one that goes out next — `queue_priority` alone. The
   *  ⤒ glyph's condition, and the condition a surface disables Skip on. */
  runsNext: boolean;
  /** The holder's title, for a tooltip. "" when the server named none. */
  aheadTitle: string;
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
 * at #1 looks like the same sentence, and it is not one: a position is where
 * this stood when the server last looked, and anything else in the folder can
 * be skipped over it in the next second. Only the flag is a CLAIM on the spot.
 * Reading #1 as the head told the reader "runs next" about a place they might
 * lose — and, worse, took away the one control that would have made it true, by
 * drawing the ⤒ and killing Skip on the row that most wanted to press it
 * (browser QA, 2026-09-12).
 *
 * So #1 without the flag reads "#1 in line" with Skip LIVE, and pressing it
 * locks the spot; 0 (the server could not place this at all) reads a bare "in
 * line", also with Skip live. Both are one idempotent call away from the flag
 * they are missing, which is the cheap direction to be wrong in.
 */
export function queueRunsNext(facts: QueueFacts): boolean {
  return facts.queue_priority === true;
}

/**
 * The caption, or null when this is not queued at all.
 *
 * "behind a run in this folder" IS THE EMPTY CASE, not a blank. A folder can be
 * held by something with no task row to name — a scheduler entry already
 * claimed, a run whose transcript has gone — and a caption trailing off after
 * "behind" would read as a bug rather than as the honest "something".
 *
 * The status test is a plain `!== "queued"` rather than the shell's
 * `statusColumn` narrowing, which is not importable from here. The two agree on
 * the only value this asks about: an unknown status is not `"queued"` either way.
 *
 * "runs next" IS THE SKIPPED CASE ONLY, and a bare "in line" the unplaced one
 * (`queue_position` 0): "#0 in line" is not a place, and "runs next" would be a
 * claim about a spot nobody has taken — see `queueRunsNext`.
 *
 * `behind_own` REPLACES BOTH HALVES OF THE "stranger's run" READING: "after
 * your previous message" instead of "behind TASK-041", and NOTHING at all in
 * front of that phrase — placed or not — because the phrase IS the position
 * and a "#n" beside it counts a line the reader is not standing in. Only an
 * admission answer ever sets it (see the field).
 */
export function queueLine(facts: QueueFacts): QueueLine | null {
  if (facts.status !== "queued") return null;
  const runsNext = queueRunsNext(facts);
  const at = queuePosition(facts);
  const own = facts.behind_own === true;
  // NO POSITION AT ALL FOR A FOLLOW-UP, placed or not (browser QA round 2,
  // 2026-09-12). "#1 in line · after your previous message" was the reading
  // that sent this back: the second half already IS the position — it names the
  // exact thing in front, which is the one message the reader can see for
  // themselves — so a number in front of it either repeats it ("#1") or
  // contradicts it, because the folder's line counts a stranger's tasks the
  // reader is not behind. The head is dropped and the sentence reads
  // "after your previous message"; a SKIPPED follow-up keeps "runs next",
  // which is a claim about the spot rather than a count of it.
  //
  // Everywhere else the bare "in line" is still the honest half of a caption
  // whose other half names a stranger's run.
  const head = runsNext ? "runs next" : own ? "" : at > 0 ? `#${at} in line` : "in line";
  const ahead = (facts.queue_ahead || "").trim();
  // …AND THE SAME ORDER OF PREFERENCE AS THE HEAD: the reader's own message
  // first, because a chat that queued two sends is the one place where naming
  // some TASK-nnn would send them looking for a task they do not have.
  const behind = own
    ? "after your previous message"
    : ahead
      ? `behind ${ahead}`
      : "behind a run in this folder";
  return {
    head,
    behind,
    text: head ? `${head} · ${behind}` : behind,
    runsNext,
    aheadTitle: (facts.queue_ahead_title || "").trim(),
  };
}

/**
 * The mark a row or card wears when its work is the next out of its folder.
 *
 * AN ARROW TO A BAR, and not a star or a bolt. It means "to the top of this",
 * which is exactly what skipping the queue does and is the only thing it does:
 * the run holding the folder keeps running. A lightning glyph would promise the
 * one thing this feature must never be read as offering.
 */
export const QUEUE_PRIORITY_GLYPH = "⤒";
