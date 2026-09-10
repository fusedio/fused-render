// THE SPLIT LAYOUT'S OVERLAY SEAT (`#annhl` + `#annpins`, T:3907-3908, CSS
// T:630-662).
//
// Only the split layout needs this: there the pane is OURS, so the ring and the
// pins are ordinary nodes of this document sitting over the frame. Hosted and XO
// build the same two boxes inside a shadow root in another document instead
// (`layer.ts`), which is why the painter (`paintPins`, `placeHl`) takes the
// boxes as arguments and does not care which of the three made them.
//
// THE COORDINATE BOX IS `.c-leftview`, NOT `.c-left` (T:6888): pins are placed in
// FRAME VIEWPORT coordinates, so their host must be the box the iframe fills —
// `.c-left` also holds the mode bar and the left-mode row, and measuring that
// would put every pin their height too high.
//
// The nodes are created imperatively into the pane's view box, because
// `pane/AppPane` renders that box and is not this PR's to change: the integrator
// hands the element in (`stage`), and everything the annotation layer needs of
// the pane is that one reference.
import { useEffect, useRef } from "react";

import "../styles/ann.css";

export interface AnnPinsProps {
  /** `.c-leftview` — the frame's box. Null before the pane resolves, and for
   *  every layout that is not the split one. */
  stage: HTMLElement | null;
  /** Handed the two live boxes (and the stage) so the coordinator can bind them
   *  as the current layer. `null` on unmount or with no stage. */
  onBind?: (bind: { pins: HTMLElement; hl: HTMLElement; stage: HTMLElement } | null) => void;
}

export function AnnPins({ stage, onBind }: AnnPinsProps) {
  const live = useRef(onBind);
  live.current = onBind;

  useEffect(() => {
    if (!stage) {
      live.current?.(null);
      return;
    }
    const doc = stage.ownerDocument;
    const hl = doc.createElement("div");
    hl.className = "c-annhl";
    const pins = doc.createElement("div");
    pins.className = "c-annpins";
    // Ring first, pins second, and both AFTER the frame: painted in tree order,
    // so a pin is never hidden behind the ring of the element it marks, and
    // neither is behind the iframe (T:6512). The page sheet gives them their
    // z-indexes; the order is what makes those two numbers enough.
    stage.append(hl, pins);
    live.current?.({ pins, hl, stage });
    return () => {
      live.current?.(null);
      hl.remove();
      pins.remove();
    };
  }, [stage]);

  return null;
}

export default AnnPins;
