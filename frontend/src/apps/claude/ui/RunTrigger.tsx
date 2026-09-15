// The word that opens a folded run of tool calls (design.md §A) — and the whole
// of the machinery a settled turn shows inline.
//
// v1 drew a chip header (`N tool calls`) over the run, which is an accordion
// row in the prose column: the reader who wanted the reply still read a row of
// UI between two paragraphs, and the count was a number nobody acts on. This is
// the same disclosure with the row removed — two muted characters at the RIGHT
// EDGE of the sentence the run follows, in the last line's own box, so the
// transcript's left rail stays prose from top to bottom.
//
// `more` / `less`, never a count (design.md §A vocabulary). The chevron follows
// the word and turns with the state, because the word alone at 11px muted is not
// obviously a control; it is `aria-hidden` and the button's own text is its
// name.
import { cn } from "@platform/lib/utils";

export interface RunTriggerProps {
  /** Is the run it belongs to open? */
  open: boolean;
  /** The reader's click — `useCardOpen`'s toggle, held by the run. */
  onToggle: () => void;
  /** Extra class for the seat (`is-bare`'s own line). */
  className?: string;
}

export function RunTrigger({ open, onToggle, className }: RunTriggerProps) {
  return (
    <button
      type="button"
      className={cn("run-trigger", className)}
      aria-expanded={open}
      onClick={onToggle}
    >
      {open ? "less" : "more"}
      <span className="run-chev" aria-hidden="true">
        {open ? "▾" : "▸"}
      </span>
    </button>
  );
}

export default RunTrigger;
