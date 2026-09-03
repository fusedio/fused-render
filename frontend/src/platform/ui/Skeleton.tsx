// A few shimmer bars standing in for a block of content while it loads, on the
// shadcn Skeleton primitive (re-exported here for callers that want one bar).
// Deliberately approximate: a handful of ragged bars reads as "content is
// coming" without pretending to be a pixel-accurate ghost of the real layout.
//
// aria-busy + a label so a screen reader gets what the "Loading…" text used to
// say; the bars themselves are decoration.
import { cn } from "@platform/lib/utils";
import { Skeleton } from "@platform/shadcn/ui/skeleton";

export { Skeleton };

const DEFAULT_WIDTHS = [72, 54, 63];

export function SkeletonLines({
  rows = 3,
  widths = DEFAULT_WIDTHS,
  label = "Loading",
  className,
}: {
  rows?: number;
  // Bar widths as percentages of the container, cycled when there are more rows
  // than entries.
  widths?: number[];
  label?: string;
  // The caller's PLACEMENT of the block, not its look: a page that used to
  // reach in with `[&>.skel-lines]:m-4` (AppFiles) has no class to select on
  // any more, so it passes the margin instead.
  className?: string;
}) {
  return (
    <div
      className={cn("flex flex-col gap-2 py-1", className)}
      role="status"
      aria-busy="true"
      aria-label={label}
    >
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton
          key={i}
          className="h-3.5 motion-reduce:animate-none"
          style={{ width: `${widths[i % widths.length]}%` }}
        />
      ))}
    </div>
  );
}

export default SkeletonLines;
