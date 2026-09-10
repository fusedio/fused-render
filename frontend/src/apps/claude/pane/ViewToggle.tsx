// The narrow layout's Chat ⇄ Preview toggle (`#viewbtn`, T:4066, 8940-8957).
//
// It is NAVIGATION, not a mode: the annotate switch is still what arms the mode
// once you are in the preview, and merging the two would make one click both
// move the view and change what a click in the frame does.
//
// Three things about its seat, all load-bearing (T:3833-3843):
//   * it must be reachable from BOTH views — it is the way out of each, so it is
//     the one control the narrow rules never hide;
//   * it is the control strip's LAST word in the wide DOM order, which is why
//     the left-mode picker inserts itself BEFORE it when the narrow layout moves
//     the picker into the shared strip (LeftModePicker's placement rules);
//   * `order: -1` in the preview view puts it at the row's left edge — the way
//     out of a view sits where the way back always sits.
//
// Removed, not disabled, when there is no pane (`enterNoPane`) — which in React
// means the parent simply does not render it.
import type { NarrowViewState } from "./useNarrowView";

export interface ViewToggleProps {
  /** From `useNarrowView`. */
  narrowView: Pick<NarrowViewState, "label" | "toggle">;
}

export function ViewToggle({ narrowView }: ViewToggleProps) {
  return (
    <button
      type="button"
      className="c-viewbtn"
      // ONE string for the label and the aria-label: a second wording is a
      // second thing to keep in step, and there is nothing the screen reader
      // needs that the visible text does not already say (T:8884).
      aria-label={narrowView.label}
      // The TITLE is the one exception, and T:4060-4062 ships it verbatim. The
      // label names the DESTINATION ("Comment on preview"); this sentence is
      // the only place that says WHY the other view matters — that the
      // annotation tools live over there. A reader who has never armed a mode
      // has no other way to learn it, so the hover keeps the sentence that
      // explains the feature rather than repeating the button.
      title="Switch between the chat and the preview pane, where the annotation tools are"
      onClick={narrowView.toggle}
    >
      {narrowView.label}
    </button>
  );
}
