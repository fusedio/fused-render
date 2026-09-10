// The 4px drag handle between the pane and the chat (`#divider`, T:3926).
//
// A real `role="separator"` with `aria-orientation="vertical"` and a name, as
// the template ships it — the control is otherwise an unlabelled sliver. It
// carries no keyboard behaviour here for the same reason it carried none there:
// the ratio is a param, and Back/Forward plus a fresh drag are the two ways it
// changes. Hidden by CSS below the breakpoint (`.chat-root.narrow`), where there
// is nothing to drag.
import type { SplitState } from "./useSplit";

export interface SplitDividerProps {
  /** From `useSplit`. */
  split: Pick<SplitState, "onPointerDown">;
}

export function SplitDivider({ split }: SplitDividerProps) {
  return (
    <div
      className="c-divider"
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize panels"
      onPointerDown={split.onPointerDown}
    />
  );
}
