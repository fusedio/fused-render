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
// AND IT IS THE TURN'S NUMBER, NOT THE RUN'S (Akshil, 2026-10-05: "how
// accurate is this? confirm with Claude on real sessions"). Claude's figure is
// the whole turn — the user's message to the end of the reply — and that is
// what `duration_ms` on the CLI's own `result` row measures. The span of the
// folded tool calls alone was half of it (12 s of a 24 s turn). So the start
// is the USER turn's stamp (`startedAt`) and the end is the last stamped thing
// in the reply: a tool's result, or the end of the final streamed text
// (`ended`, agent.py). Checked on 25 real turns against `duration_ms`: mean
// difference 0.06 s, worst 1.2 s. Each half is dropped when the rows did not
// say: no end stamp at all (a turn still streaming) means nothing is drawn
// rather than a guess; no `startedAt` falls back to the reply's own first
// stamp, which under-counts and is the best an old transcript allows.
import { ago } from "../protocol/history";
import type { Segment } from "../protocol/types";

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

/** `Worked for 2m 10s · 3m ago` — the hover, for the whole turn `segs` belong
 *  to. `startedAt` is the user turn's stamp (epoch seconds); "ago" is measured
 *  from the END, because "when was it done" is the question. `ago`'s bare
 *  `now` becomes `just now`, the word the turn stamps already use. */
export function runWhenWords(
  segs: readonly (Segment | null | undefined)[],
  startedAt: number | null | undefined = null,
  now: () => number = Date.now,
): string | null {
  const span = runSpan(segs);
  if (!span) return null;
  const started =
    typeof startedAt === "number" && Number.isFinite(startedAt) && startedAt > 0 && startedAt <= span.finished
      ? startedAt
      : span.started;
  const when = ago(span.finished, now);
  const done = when === "now" ? "just now" : when;
  return span.finished > started
    ? "Worked for " + spanWords(span.finished - started) + " · " + done
    : done;
}
