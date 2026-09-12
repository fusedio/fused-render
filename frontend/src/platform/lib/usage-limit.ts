// A SESSION THE PLAN'S USAGE LIMIT STOPPED, IN WORDS — "Usage limit · resumes
// 4:00 AM" on a task row, "paused · resumes 4:00 AM" at the top of its chat.
//
// WHY IT IS NOT "blocked". The server files a usage-limited session under
// `blocked` (`Task.status`) with `blocked_reason: "usage_limit"` and the instant
// the window reopens in `resumes_at`, because for every view that sorts and
// colours by lane it IS blocked: nothing is moving and nothing will move by
// itself. But "Blocked" is the word this app uses for work that BROKE, and this
// did not break — it is waiting for a clock, and the clock is known. So the lane
// and the ring stay exactly as they are (red, Blocked, beside the runs that
// failed) and the CAPTION says the one thing that makes this row different from
// the failures around it: what stopped it, and when it starts again.
//
// PURE, AND IN PLATFORM, for `lib/queue.ts`'s reason: the Tasks List, the Board
// card and the Cards wall are shell, the chat's header is an app, and an app may
// not import shell (scripts/check-boundaries.mjs). One wording, one module, both
// layers.

/** The value `Task.blocked_reason` carries for a session the plan's limit
 *  stopped. The server's word, restated here so no surface types the string. */
export const USAGE_LIMIT_REASON = "usage_limit";

/** The row fields this reads — the subset of `Task` that says "stopped on the
 *  limit, and when it comes back". A caller holding a pulse row rather than a
 *  listing row can ask the same question of it. */
export interface UsageLimitFacts {
  status?: string;
  blocked_reason?: string | null;
  /** Epoch SECONDS the plan's window reopens, as the CLI reported it
   *  (`rate_limit_event.resetsAt`). 0 or absent is an honest "the server could
   *  not say", and then the sentence stops after "Usage limit". */
  resumes_at?: number | null;
}

/** Is this row a session the plan's usage limit stopped? Both halves, because
 *  neither alone is the fact: `blocked` is every kind of not-moving, and a
 *  reason on a row that is running again is a leftover. */
export function isUsageLimited(facts: UsageLimitFacts | null | undefined): boolean {
  if (!facts) return false;
  return facts.status === "blocked" && (facts.blocked_reason || "") === USAGE_LIMIT_REASON;
}

/**
 * "4:00 AM" — the reopening instant on the reader's own clock.
 *
 * SPELLED HERE rather than through `toLocaleTimeString`, so the words are the
 * same in every runtime a test and a browser can disagree about — and so this
 * module can be pinned by a string rather than by whatever locale the machine
 * running the suite happens to hold. The ZONE is still the reader's: the hour
 * and minute come off a local `Date`.
 *
 * "" for 0, for absent and for anything unreadable, which is what lets the
 * callers below drop the clause instead of printing a clock nobody can trust.
 */
export function resumesClock(epochSeconds: number | null | undefined): string {
  const at = Number(epochSeconds ?? 0);
  if (!Number.isFinite(at) || at <= 0) return "";
  const d = new Date(at * 1000);
  if (Number.isNaN(d.getTime())) return "";
  const mins = d.getMinutes();
  const hours = d.getHours();
  const suffix = hours >= 12 ? "PM" : "AM";
  const hour12 = hours % 12 === 0 ? 12 : hours % 12;
  return `${hour12}:${mins < 10 ? "0" : ""}${mins} ${suffix}`;
}

/** The clause both sentences end with — " · resumes 4:00 AM", or "" when the
 *  server named no instant. */
function resumesClause(facts: UsageLimitFacts): string {
  const clock = resumesClock(facts.resumes_at);
  return clock ? ` · resumes ${clock}` : "";
}

/**
 * THE ROW'S CAPTION — "Usage limit · resumes 4:00 AM".
 *
 * Named rather than explained: a reader scanning the Blocked lane needs to know
 * which of these rows is a failure and which is a clock, and two words do it.
 * "" on every other row, so the caller draws nothing rather than an empty line.
 */
export function usageLimitCaption(facts: UsageLimitFacts | null | undefined): string {
  if (!isUsageLimited(facts)) return "";
  return "Usage limit" + resumesClause(facts as UsageLimitFacts);
}

/**
 * THE CHAT HEADER'S WORD — "paused · resumes 4:00 AM", where a live chat says
 * "running".
 *
 * PAUSED, not "Usage limit": the header is about THIS conversation and the
 * reader is inside it, where the question is "why is nothing happening" and the
 * answer is "it will start again by itself, at this time". The row's caption is
 * read across a list of other tasks, where naming the cause is what tells it
 * apart from the failures beside it.
 */
export function usageLimitStatusWord(facts: UsageLimitFacts | null | undefined): string {
  if (!isUsageLimited(facts)) return "";
  return "paused" + resumesClause(facts as UsageLimitFacts);
}
