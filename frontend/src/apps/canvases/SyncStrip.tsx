// The workspace's status strip: one dense bordered row above the two panes,
// used for both the lock banner (syncing / advisory) and the push-error
// banner. Colour comes only from status-colors via StatusBadge; the strip's
// own chrome is neutral so the badge is the one chromatic thing on it.
import type { ReactNode } from "react";
import { cn } from "@platform/lib/utils";
import { StatusBadge } from "@platform/ui/flow/StatusIcon";
import type { StatusBucket } from "@platform/ui/status-colors";

export function SyncStrip({
  bucket,
  badge,
  children,
  detail,
  className,
  ...rest
}: {
  bucket: StatusBucket;
  /** Short state word shown in the badge (e.g. "Syncing", "Error"). */
  badge: string;
  children: ReactNode;
  /** Optional second line under the row (error detail, etc). */
  detail?: ReactNode;
  className?: string;
  "data-testid"?: string;
}) {
  return (
    <div
      role="status"
      className={cn("border-b border-border bg-card px-3 py-2 text-sm", className)}
      {...rest}
    >
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge bucket={bucket}>{badge}</StatusBadge>
        {children}
      </div>
      {detail}
    </div>
  );
}
