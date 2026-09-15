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
// `show more` / `show less`, never a count (design.md §A vocabulary). Two words
// rather than one (Akshil, 2026-09-15): `more` alone in a paragraph's corner
// reads as the end of the sentence it is sitting on.
//
// AND THE WORDS ARE THE WHOLE CONTROL (Akshil, 2026-09-15). A chevron followed
// the word for one release, on the theory that muted words are not obviously
// pressable — but the state is already IN the words (`more` vs `less`), so the
// glyph said the same thing twice, in the one piece of the trigger that is not
// prose: a geometric shape at the end of a sentence, at a different weight to
// every character around it, in the corner the eye lands on last. Removing it
// leaves two words in the reading type and a colour that lifts on hover, which
// is how every other inline affordance in this transcript reads.
//
// AND IT IS SET IN THE PROSE'S OWN TYPE (Akshil, 2026-09-15): same font-size,
// same line-height, inherited from the block (`styles/transcript.css`), so the
// word sits ON the last line's baseline instead of floating in a smaller box
// beside it. Muted colour is what keeps it out of the reading, not small type.
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
      {open ? "show less" : "show more"}
    </button>
  );
}

export default RunTrigger;
