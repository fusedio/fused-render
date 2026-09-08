// The settings fold: the work column's two tracks, the rail the settings card
// travels in, and the card that sticks inside it.
//
// The parameters are a narrow CARD of their own rather than a bare band,
// because at anything but a phone width it sits BESIDE the column — the two
// centered together — and a floating stack of sliders with no edge would read
// as loose page furniture. Two boxes and not one: beside the column the
// <aside> is a full-height rail and the inner box is what sticks inside it
// (sticky cannot live on an absolutely positioned box).
//
// Three of the fold's rules are beyond a Tailwind utility — a transition on
// `grid-template-columns`, a `:has()` that holds the open geometry through the
// exit, and a container query — so `.stage-work-grid`, `.stage-config-rail`
// and `.stage-config-inner` are declared in styles/tailwind.css, next to the
// keyframes they use. Everything else here is a utility, and the timings stay
// tokenised on `--pg-glide` / `--pg-fade`.
import type { ComponentProps } from "react";

import { cn } from "@platform/lib/utils";

/** The work column. `open` reserves the settings track — the column's own box
 *  never changes, so the bottom of the stage stays in sync with the top and
 *  what the cog changes is only the TRACKS inside it. */
export function workGridClass(open: boolean, className?: string): string {
  return cn("stage-work-grid", open && "is-open", className);
}

/** The work Card inside that grid. It needs `overflow: visible` on the wide
 *  layout so the rail escapes into the side track instead of being clipped —
 *  the rule is keyed on this class. */
export const stageWorkCardClass = "stage-work-card";

/** The card the rail carries. Named so the sticky, fold-in/out and
 *  reduced-motion rules keep landing on it. */
export const configCardClass = "stage-config-inner";

export function ConfigRail({
  /** Fading out: a picture of a panel, not a panel — a click landing on a
   *  control already on its way out is a setting changed by accident. */
  closing,
  /** False on a mount the user did not cause: the fold-in and its wait beside
   *  the column are skipped, the card is just there. */
  animated = true,
  className,
  ...props
}: ComponentProps<"aside"> & { closing?: boolean; animated?: boolean }) {
  return (
    <aside
      className={cn(
        "stage-config-rail flex flex-col",
        closing && "is-closing pointer-events-none",
        !animated && "no-entry",
        className,
      )}
      {...props}
    />
  );
}

/** Chip rows lead the fold, tighter than the slider stack below them. */
export function ConfigChips({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("flex flex-col gap-2", className)} {...props} />;
}
