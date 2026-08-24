// One drawing of "this model is busy", shared by every card on /ai-models that
// can be waiting for one.
//
// It exists because those cards ask the same question from opposite sides: a
// repo this machine already has coming into memory, and a repo it does not have
// — a recommendation, or a Hub search result — arriving on disk. Same job row,
// same bytes, same bar — and when they were drawn separately, one of them said
// "Downloaded" while the other was still counting.
//
// The byte counts are the JOB's, never the runtime's. The runtime knows what is
// happening; only the worker doing the fetching knows how far it has got, and it
// reports that to the download manager (SPEC §36) under a deterministic id.
import { formatSize } from "@platform/lib/format";
import type { Job } from "@platform/lib/jobs";

/**
 * `stop` is the way OUT of the work this row is reporting, drawn at the END of
 * the row (Akshil, 2026-08-24).
 *
 * It used to be a sibling of the card's Load/Unload button, up in the actions
 * strip, and the report was that it could not be hit: "I don't notice it and I
 * cannot click it because it is fast when I'm trying to load it". Both halves of
 * that are the position, not the button. Up there it appeared and disappeared
 * with the job, which SHIFTED the strip it lives in — so the target moved while
 * being aimed at — and it sat beside a button reading `Unload`, which is a
 * different act on a card that has not finished loading yet.
 *
 * Here it is attached to the thing it stops. This row is only ever drawn while
 * work is in flight, so a control inside it costs no layout when there is none,
 * and its neighbour is the bar rather than an unrelated verb. One shape for both
 * kinds of work: a download and a load now report and cancel identically, which
 * is what the ask asked for ("follow the same UI when loading as well").
 */
export function ModelProgress({
  detail,
  job,
  stop,
}: {
  detail?: string | null;
  job?: Job;
  /** Label + handler for the trailing stop control. Omitted where the work
   *  cannot be stopped — a `uv sync` mid-build has nothing safe to interrupt —
   *  and the row then draws exactly as it always did. */
  stop?: { label: string; onStop: () => void };
}) {
  const text = detail || job?.detail || "Preparing…";
  // A bar only when there is a real total to divide by. A download knows its
  // size; a venv build and a weight load do not, and an invented percentage on
  // those is what makes live work read as frozen.
  // `!!`, not the raw chain: `job.total` of 0 makes `&&` yield the NUMBER 0,
  // and React renders a literal "0" for it rather than nothing.
  const bytes = !!(job && job.unit === "bytes" && job.total && job.done !== null);
  const pct = job && job.total && job.done !== null ? Math.min(100, (job.done / job.total) * 100) : null;
  return (
    <div className="am-card-runtime">
      <span className="am-runtime-dot" />
      <span className="am-runtime-detail">{text}</span>
      {/* MEASURED when there is something to measure, INDETERMINATE when there
          is not (2026-08-24) — and the second one is new. A load reports no
          total, so this row used to be a dot and a word for however long the
          weights took, which reads as a card that has stopped rather than one
          that is working. The stripe is the honest middle: it says "moving"
          without claiming a fraction, which is the distinction the comment above
          draws and the reason there is still no invented percentage here.
          Same bar, same track, same width — only the fill differs — so a load
          and a download are one drawing and the row never changes size. */}
      <span className="am-runtime-bar">
        {pct === null ? (
          <span className="am-runtime-bar-fill am-runtime-bar-indeterminate" />
        ) : (
          <span className="am-runtime-bar-fill" style={{ width: `${pct}%` }} />
        )}
      </span>
      {bytes && (
        <span className="am-runtime-mem">
          {formatSize(job.done as number)} / {formatSize(job.total as number)}
        </span>
      )}
      {stop && (
        <button
          type="button"
          className="cc-iconbtn am-runtime-stop"
          title={stop.label}
          aria-label={stop.label}
          onClick={stop.onStop}
        >
          ✕
        </button>
      )}
    </div>
  );
}
