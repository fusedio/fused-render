// The transcript, as timed lines. A scroller of its own rather than a growing
// page: a ten-minute recording is two hundred rows, and the stage's own
// scrollbar would put the composer off screen.
import type { ComponentProps } from "react";

import { cn } from "@platform/lib/utils";

/** The right padding keeps the first line clear of the copy button pinned in
 *  the corner. */
export function SegmentList({ className, ...props }: ComponentProps<"div">) {
  return (
    <div
      className={cn(
        "relative flex min-h-[140px] flex-1 flex-col gap-[5px] overflow-y-auto rounded-[12px] border border-solid border-[var(--border)] bg-[var(--bg-alt)] pt-3 pr-[44px] pb-3 pl-[14px]",
        className,
      )}
      {...props}
    />
  );
}

export function Segment({ className, ...props }: ComponentProps<"div">) {
  return (
    <div className={cn("flex gap-3 text-[13px] leading-[1.55]", className)} {...props} />
  );
}

/** A fixed 42px column, tabular: the times are a ruler down the left edge, and
 *  a ragged one is not a ruler. */
export function SegmentTime({ className, ...props }: ComponentProps<"span">) {
  return (
    <span
      className={cn(
        "w-[42px] flex-none pt-0.5 text-[11.5px] text-[var(--fg-muted)] tabular-nums",
        className,
      )}
      {...props}
    />
  );
}

export function SegmentText({ className, ...props }: ComponentProps<"span">) {
  return <span className={cn("min-w-0", className)} {...props} />;
}

/** The joined transcript, when a finished run has no segment view to draw —
 *  and, `empty`, the two distinct "nothing came back" sentences. */
export function TranscriptText({
  empty,
  className,
  ...props
}: ComponentProps<"p"> & { empty?: boolean }) {
  return (
    <p
      className={cn(
        "m-0 text-[13px] leading-[1.6] whitespace-pre-wrap",
        empty && "text-[var(--fg-muted)]",
        className,
      )}
      {...props}
    />
  );
}
