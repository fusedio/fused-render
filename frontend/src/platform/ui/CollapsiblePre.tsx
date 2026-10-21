// A card payload (`<pre>`) that is never a scroller of its own.
//
// WHY NOT `max-height` + `overflow: auto` ANY MORE (2026-10-12). Every card
// payload used to be capped at 190px and scrolled inside itself. The card
// already sits in the pinned tail, which is the ONE scroller allowed there
// (`.chat-tailpin.is-pinned`, transcript.css), so a long Bash command made a box
// inside a box on the same axis — and a trackpad swipe latches on whichever box
// the pointer starts over. The swipe got stuck in the `<pre>`, the card never
// moved, and Allow/Deny stayed out of reach below it.
//
// So a long payload is CLIPPED, not scrolled: the first `COLLAPSED_LINES` lines,
// a fade that says there is more, and a real button that opens the rest in
// place. Clipped is still honest — the card is the prompt, and the user must be
// able to see exactly what they approve — because the truncation is visible
// (fade + "N more lines") and the full text is one click away.
//
// Two ways to be long, both measured: more than `COLLAPSED_LINES` newlines
// (counted, so the button can say how many), or few newlines that WRAP past the
// cap (one 2000-char line). The second has no line count to report, so the
// button just says "Show full …". Expanded is per card, never persisted.
import { useId, useLayoutEffect, useRef, useState } from "react";

import { cn } from "@platform/lib/utils";

/** Lines shown while collapsed. Mirrored by `--perm-pre-lines` in
 *  transcript.css, which turns it into the clip height. */
export const COLLAPSED_LINES = 6;

/** Lines past the cap, counted by newline. A trailing newline is not a line. */
export function hiddenLineCount(text: string): number {
  const lines = text.replace(/\n$/, "").split("\n").length;
  return Math.max(0, lines - COLLAPSED_LINES);
}

/** The disclosure button's words. Exported for the tests. */
export function toggleLabel(what: string, hidden: number, expanded: boolean): string {
  if (expanded) return "Show less";
  if (hidden > 0) return `Show full ${what} (${hidden} more ${hidden === 1 ? "line" : "lines"})`;
  return `Show full ${what}`;
}

export interface CollapsiblePreProps {
  text: string;
  /** Noun for the button: "Show full {what}". */
  what?: string;
}

/** Keyed on the text, so a payload that changes starts collapsed and
 *  re-measured rather than inheriting the old one's expanded/wraps state. */
export function CollapsiblePre(props: CollapsiblePreProps) {
  return <CollapsiblePreInner key={props.text} {...props} />;
}

function CollapsiblePreInner({ text, what = "command" }: CollapsiblePreProps) {
  const id = useId();
  const ref = useRef<HTMLPreElement>(null);
  const [expanded, setExpanded] = useState(false);
  // Wrapped overflow only — newline overflow is known without a DOM.
  const [wraps, setWraps] = useState(false);
  const hidden = hiddenLineCount(text);

  // Measured only while collapsed: that is when the clip is on, so
  // `scrollHeight > clientHeight` means "something is cut off". Once expanded
  // the last verdict stands, so "Show less" does not vanish under the reader.
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el || expanded || hidden > 0) return;
    const measure = () => setWraps(el.scrollHeight > el.clientHeight + 1);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    // The fade and the button sit outside the pre's box (the fade is an
    // absolutely positioned pseudo), so the write here never resizes the
    // observed element and cannot loop.
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [text, expanded, hidden]);

  const collapsible = hidden > 0 || wraps;
  const clipped = collapsible && !expanded;

  return (
    <>
      <pre
        ref={ref}
        id={id}
        className={cn("perm-pre", !expanded && "is-collapsed", clipped && "is-clipped")}
      >
        {text}
      </pre>
      {collapsible ? (
        <button
          type="button"
          className="perm-pre-toggle"
          aria-expanded={expanded}
          aria-controls={id}
          onClick={() => setExpanded((v) => !v)}
        >
          {toggleLabel(what, hidden, expanded)}
        </button>
      ) : null}
    </>
  );
}

export default CollapsiblePre;
