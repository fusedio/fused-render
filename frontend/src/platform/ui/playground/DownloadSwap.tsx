// ONE CELL, TWO DRAWINGS — a running pull's progress (ring + byte figures) and
// the Cancel that replaces it under the pointer. Stacked in a single grid area
// rather than swapped in flow, so the box measures the wider of the two at rest
// and hover changes nothing but which one is painted; a slot that resized would
// shove the buttons beside it sideways at the moment the pointer arrived.
//
// Opacity and not `display`, for two reasons: a display-toggled button cannot
// be focused, so `:focus-within` would never fire and the way out of a download
// would be pointer-only; and a box that stops laying out its child stops
// reserving its size, which is the reflow this grid exists to prevent.
// `pointer-events` is what keeps the invisible half from swallowing clicks
// meant for the visible one.
import type { ComponentProps } from "react";

import { cn } from "@platform/lib/utils";

import { INHERIT_FONT_ALL } from "./classes";

/** The whole widget — icon, swap, and the `group` the two halves watch. The
 *  download icon sits OUTSIDE the swap, because it does not take part. */
export function DownloadSwapRoot({ className, ...props }: ComponentProps<"div">) {
  return (
    <div
      className={cn(
        "group flex items-center gap-2 self-center text-[12px] text-[var(--fg-muted)]",
        className,
      )}
      {...props}
    />
  );
}

export function DownloadSwapIcon({ className, ...props }: ComponentProps<"span">) {
  return (
    <span
      className={cn("inline-flex flex-none [&_svg]:h-3.5 [&_svg]:w-3.5", className)}
      aria-hidden="true"
      {...props}
    />
  );
}

/** The single grid area both drawings share. */
export function DownloadSwap({ className, ...props }: ComponentProps<"span">) {
  return (
    <span
      className={cn("grid items-center justify-items-start [&>*]:[grid-area:1/1]", className)}
      {...props}
    />
  );
}

/** The progress half: visible at rest, faded out under the pointer. */
export function DownloadSwapLive({ className, ...props }: ComponentProps<"span">) {
  return (
    <span
      className={cn(
        "flex items-center gap-2 transition-opacity duration-[120ms] ease-[ease]",
        "group-hover:pointer-events-none group-hover:opacity-0",
        "group-focus-within:pointer-events-none group-focus-within:opacity-0",
        className,
      )}
      {...props}
    />
  );
}

/** The way out. A word the size of the figures it stands in for, in the error
 *  hue at 85% so it reads as pale rather than as an alarm — this is an offer,
 *  not a failure. No border, no padding, no background: it replaces 12px of
 *  muted text in the corner of a card, and a bordered control there would
 *  out-shout everything including the download it stops. The underline is what
 *  says it is pressable. */
export function DownloadSwapStop({ className, ...props }: ComponentProps<"button">) {
  return (
    <button
      className={cn(
        "pointer-events-none cursor-pointer border-none bg-transparent p-0 opacity-0",
        INHERIT_FONT_ALL,
        "text-[rgba(var(--error-rgb),0.85)] underline underline-offset-2 hover:text-[var(--error)]",
        "transition-opacity duration-[120ms] ease-[ease]",
        "group-hover:pointer-events-auto group-hover:opacity-100",
        "group-focus-within:pointer-events-auto group-focus-within:opacity-100",
        className,
      )}
      {...props}
    />
  );
}

/** Tabular, because these two numbers are re-rendered every second and
 *  proportional digits make a still row look like a shivering one. */
export function DownloadSwapBytes({ className, ...props }: ComponentProps<"span">) {
  return <span className={cn("whitespace-nowrap tabular-nums", className)} {...props} />;
}
