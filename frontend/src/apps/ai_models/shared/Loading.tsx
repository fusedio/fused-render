// A few skeleton bars standing in for a block while it loads — shadcn Skeleton,
// with the aria-busy/label contract the old SkeletonLines carried.
import { Skeleton } from "@platform/shadcn/ui/skeleton";

const WIDTHS = ["72%", "54%", "63%"];

export function Loading({ rows = 3, label = "Loading" }: { rows?: number; label?: string }) {
  return (
    <div className="flex flex-col gap-2 py-2" role="status" aria-busy="true" aria-label={label}>
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} className="h-3" style={{ width: WIDTHS[i % WIDTHS.length] }} />
      ))}
    </div>
  );
}
