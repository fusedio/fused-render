// The List / Board / Cards / Calendar switch: one bordered box, its halves laid
// out by their text, the selected half carrying a fill and nothing else — no
// weight change, because bolding re-measures every half and the toggle jitters
// (audit 2026-08-16). Callers keep their own <button> attributes: `aria-pressed`
// is the state, `data-view`/`onClick` are theirs, and the `schedule-view-btn`
// class stays as the hook the glyph rules in schedule.css hang off.
//
// No Tailwind preflight in this app: the button reset (UA plate, border,
// margin) is stated here once rather than in a page sheet.
import type { ComponentProps } from "react";

import { cn } from "@platform/lib/utils";

export function SegmentedControl({ className, ...props }: ComponentProps<"div">) {
  return (
    <div
      className={cn(
        "inline-flex overflow-hidden rounded-control border border-solid border-border",
        className,
      )}
      {...props}
    />
  );
}

export function SegmentButton({ className, ...props }: ComponentProps<"button">) {
  return (
    <button
      type="button"
      className={cn(
        // reset (no preflight)
        "m-0 cursor-pointer appearance-none bg-transparent font-[inherit] text-foreground",
        // the .btn chassis, minus its own border and radius (the box has them)
        "inline-flex h-8 items-center gap-1.5 border-0 border-solid border-border px-3.5 text-dense leading-[normal]",
        // halves meet on a hairline
        "[&+&]:border-l",
        // hover = a wash between --bg-alt and --bg; active = --bg-alt (the fill IS the state)
        "hover:bg-[color-mix(in_srgb,var(--bg-alt)_60%,var(--bg))] aria-pressed:bg-[var(--bg-alt)]",
        // overflow:hidden on the box clips an outside ring; draw it inside
        "focus-visible:outline-2 focus-visible:outline-[var(--accent)] focus-visible:-outline-offset-2",
        className,
      )}
      {...props}
    />
  );
}
