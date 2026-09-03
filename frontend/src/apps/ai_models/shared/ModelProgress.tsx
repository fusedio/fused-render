// One drawing of "this model is busy", shared by every row on /ai-models that
// can be waiting for one — a repo coming into memory, or a recommendation / Hub
// result arriving on disk. Same job row, same bytes, same bar.
//
// The byte counts are the JOB's, never the runtime's: only the worker doing the
// fetching knows how far it has got (SPEC §36).
import { DownloadIcon, XIcon } from "lucide-react";
import { Button } from "@platform/shadcn/ui/button";
import { StatusDot } from "@platform/ui/flow/StatusIcon";
import { Tiny } from "@platform/ui/flow/Typography";
import { meterFill } from "@platform/ui/status-colors";
import { cn } from "@platform/lib/utils";
import { formatSize } from "@platform/lib/format";
import type { Job } from "@platform/lib/jobs";

/** The arrow-into-a-tray every Download on this page leads with. `aria-hidden`,
 *  because every one of those buttons already says the word. */
export function DownloadGlyph() {
  return <DownloadIcon aria-hidden="true" />;
}

/**
 * `stop` is the way OUT of the work this row is reporting, drawn at the END of
 * the row: attached to the thing it stops, drawn only while work is in flight,
 * so it costs no layout when there is none (2026-08-24).
 */
export function ModelProgress({
  detail,
  job,
  stop,
  className,
}: {
  detail?: string | null;
  job?: Job;
  /** Label + handler for the trailing stop control. Omitted where the work
   *  cannot be stopped — a `uv sync` mid-build has nothing safe to interrupt. */
  stop?: { label: string; onStop: () => void };
  className?: string;
}) {
  const text = detail || job?.detail || "Preparing…";
  // A bar only when there is a real total to divide by. A download knows its
  // size; a venv build and a weight load do not, and an invented percentage on
  // those is what makes live work read as frozen.
  const bytes = !!(job && job.unit === "bytes" && job.total && job.done !== null);
  const pct = job && job.total && job.done !== null ? Math.min(100, (job.done / job.total) * 100) : null;
  return (
    <div className={cn("flex items-center gap-2 min-w-0 text-xs text-muted-foreground", className)}>
      <StatusDot status="loading" pulse />
      <span className="truncate">{text}</span>
      {/* MEASURED when there is something to measure, INDETERMINATE when there
          is not: a load reports no total, so its bar pulses instead of claiming
          a fraction. Same track, same width, so a load and a download are one
          drawing and the row never changes size. */}
      <span className="relative h-1 w-20 shrink-0 overflow-hidden rounded-full bg-muted" aria-hidden="true">
        {pct === null ? (
          <span className={cn("absolute inset-0 rounded-full motion-safe:animate-pulse", meterFill(0))} />
        ) : (
          <span className={cn("absolute inset-y-0 left-0 rounded-full", meterFill(pct))} style={{ width: `${pct}%` }} />
        )}
      </span>
      {bytes && (
        <Tiny className="shrink-0 tabular-nums">
          {formatSize(job.done as number)} / {formatSize(job.total as number)}
        </Tiny>
      )}
      {stop && (
        <Button variant="ghost" size="icon-xs" title={stop.label} aria-label={stop.label} onClick={stop.onStop}>
          <XIcon />
        </Button>
      )}
    </div>
  );
}
