// THE RIGHT-HAND PANE'S SLIDE (owner, 2026-11-05: "why is left sidebar this
// smooth transition but the right side is instant — copy same transition").
//
// The left sidebar glides because it is always mounted and its width
// transitions. The right pane is rendered as `{open && <pane/>}`, so it can only
// animate if something keeps it mounted for the length of its exit — this does.
// `phase` is the class the CSS keys off (styles/app-page.css `.is-entering` /
// `.is-leaving`): entering for one `--dur-panel` after it opens, leaving for one
// before it unmounts. Same clock as the left sidebar's collapse.
import { useLayoutEffect, useRef, useState } from "react";

/** `--dur-panel` (styles/base.css): both sidebars' open/close. */
export const PANE_SLIDE_MS = 160;

export type PanePhase = "entering" | "open" | "leaving";

export function usePanePresence(open: boolean, ms = PANE_SLIDE_MS): { mounted: boolean; phase: PanePhase } {
  const [mounted, setMounted] = useState(open);
  // Open at first paint is not an entrance: a page that loads with the pane
  // open shows it in place, like the left sidebar does.
  const [phase, setPhase] = useState<PanePhase>("open");
  const first = useRef(true);
  // A LAYOUT effect: the entering class must be on the column before its first
  // paint, or it shows one frame at full width and then starts from 0.
  useLayoutEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    setMounted(true);
    setPhase(open ? "entering" : "leaving");
    const t = setTimeout(() => {
      setPhase("open");
      if (!open) setMounted(false);
    }, ms);
    return () => clearTimeout(t);
  }, [open, ms]);
  return { mounted: open || mounted, phase };
}
