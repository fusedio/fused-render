// HOW LONG A RUN OF TOOL CALLS TOOK, AND HOW LONG AGO IT FINISHED — the words
// the `show more` trigger shows on hover.
//
// Akshil, 2026-10-04 (relaying a client): "I am trying to get a sense of how
// long the job took, which is very hard". The transcript draws a settled turn
// as prose with its machinery folded behind two words, and nothing on screen
// said when any of it happened. The segments carry their rows' clocks
// (`SegmentClock`, agent.py `_row_ts`), and this is the one place they become
// words, so nothing can drift.
//
// ONE SHORT SENTENCE, IN CLAUDE'S OWN SHAPE (Akshil, 2026-10-05: "too
// verbose"). Claude Code prints `Cooked for 2m 10s` under a finished turn and
// claude.ai folds work behind `Worked for 2m 10s`; this says the same thing
// and then how long ago it was done: `Worked for 2m 10s · 3m ago`. No wall
// clock — the turn's own stamp carries that — and no line inside the opened
// run.
//
// AND IT IS THE RUN'S NUMBER, NOT THE TURN'S (Akshil, 2026-10-06: "show more
// should only show time for things in that, like tool calls, bash time they
// took, not the whole response"). The word folds one stretch of tool calls,
// and the hover is about that stretch: its first stamp to its last result.
// A reply with two runs wears two different numbers.
import type { Segment, ToolSegment } from "../protocol/types";
import { stampTitle } from "./stamp";

/** `4s`, `2m 10s`, `1h 3m` — floor at every unit, never two decimals, and the
 *  second unit only when the first is not the whole story. */
export function spanWords(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return s + "s";
  const m = Math.floor(s / 60);
  if (m < 60) return s % 60 ? `${m}m ${s % 60}s` : `${m}m`;
  const h = Math.floor(m / 60);
  return m % 60 ? `${h}h ${m % 60}m` : `${h}h`;
}

export interface RunWhen {
  /** First stamp in the run (epoch seconds). */
  started: number;
  /** Last stamp in the run — a tool's `ended` where it has one. */
  finished: number;
}

/** The earliest and latest clock across the members, or null when not one of
 *  them carries a stamp. An `ended` (a tool's result, a streamed text's close)
 *  counts for the end; a member with no `ts` is simply skipped rather than
 *  read as 1970. */
export function runSpan(segs: readonly (Segment | null | undefined)[]): RunWhen | null {
  let started = Infinity;
  let finished = -Infinity;
  for (const seg of segs) {
    if (!seg) continue;
    const ts = seg.ts;
    if (typeof ts === "number" && Number.isFinite(ts) && ts > 0) {
      if (ts < started) started = ts;
      if (ts > finished) finished = ts;
    }
    const ended = seg.ended;
    if (typeof ended === "number" && Number.isFinite(ended) && ended > finished) finished = ended;
  }
  return Number.isFinite(started) ? { started, finished: Math.max(started, finished) } : null;
}

/** `4 Oct 2026, 22:42:33 · ran 12s` — the hover for ONE run: when its first
 *  call was made, then how long the run took, first call to last result. The
 *  SAME ORDER AS A CHIP'S STAMP HOVER (`chipHint`; Akshil, 2026-10-06: "the
 *  order is not consistent — make it follow the second one"), so the two
 *  hovers a reader meets on one run read as one sentence. */
export function runWhenWords(segs: readonly (Segment | null | undefined)[]): string | null {
  const span = runSpan(segs);
  if (!span) return null;
  const title = stampTitle(span.started);
  if (!title) return null;
  return span.finished > span.started ? title + " · ran " + spanWords(span.finished - span.started) : title;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"] as const;

/** THE LANE STAMP ON A TOOL CHIP (Akshil, 2026-10-06: "on the left side where
 *  there is space we show 1d ago, 3d ago, or some date — same language as the
 *  message stamp"). The message stamp's own words, in the message stamp's own
 *  register (ui/stamp.ts: `just now`, `3m ago`, `2h ago`), but the lane beside
 *  a chip is the ✻ mark's column plus the gutter — about 54px — so a day-old
 *  call says `4 Oct` and leaves the clock to the hover, where the message
 *  stamp has the whole row and says `4 Oct, 22:42`. Epoch seconds in. */
export function chipWhen(ts: number | null | undefined, now: number = Date.now()): string | null {
  if (typeof ts !== "number" || !Number.isFinite(ts) || ts <= 0) return null;
  const at = ts > 1e11 ? ts : ts * 1000;
  const d = new Date(at);
  if (Number.isNaN(d.getTime())) return null;
  const age = now - at;
  if (age < 60_000) return "just now";
  if (age < 3_600_000) return Math.floor(age / 60_000) + "m ago";
  if (age < 86_400_000) return Math.floor(age / 3_600_000) + "h ago";
  return d.getDate() + " " + MONTHS[d.getMonth()];
}

/** THE STAMP'S HOVER: the exact instant the call was made, and how long it ran
 *  (`4 Oct 2026, 22:42:33 · ran 4s`) — a call still running says so. */
export function chipHint(seg: Pick<ToolSegment, "ts" | "ended" | "status">): string | null {
  const title = stampTitle(seg.ts);
  if (!title) return null;
  const ts = seg.ts as number;
  const ended = seg.ended;
  if (typeof ended === "number" && Number.isFinite(ended) && ended >= ts) return title + " · ran " + spanWords(ended - ts);
  return seg.status === "running" ? title + " · still running" : title;
}
