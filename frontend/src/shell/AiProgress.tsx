// One drawing of "this model is busy", shared by both tabs of /ai-models.
//
// It exists because the two tabs ask the same question from opposite sides: the
// Cached tab shows a repo it already has coming into memory, and Discover shows
// a repo it does not have arriving on disk. Same job row, same bytes, same bar —
// and when they were drawn separately, one of them said "Downloaded" while the
// other was still counting.
//
// The byte counts are the JOB's, never the runtime's. The runtime knows what is
// happening; only the worker doing the fetching knows how far it has got, and it
// reports that to the download manager (SPEC §36) under a deterministic id.
import { formatSize } from "@platform/lib/format";
import type { Job } from "@platform/lib/jobs";

export function ModelProgress({ detail, job }: { detail?: string | null; job?: Job }) {
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
      {pct !== null && (
        <span className="am-runtime-bar">
          <span className="am-runtime-bar-fill" style={{ width: `${pct}%` }} />
        </span>
      )}
      {bytes && (
        <span className="am-runtime-mem">
          {formatSize(job.done as number)} / {formatSize(job.total as number)}
        </span>
      )}
    </div>
  );
}
