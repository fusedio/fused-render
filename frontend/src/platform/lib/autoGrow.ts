// A textarea that grows with its own text. Lives in platform because two apps
// need it — the Playground's stage composers (ai_models) and the app-building
// composer in the Home/apps hero (builder) — and an app may only import
// platform + itself. It was a hook inside the Playground's controls.tsx until
// the builder composer grew the same feature and reintroduced the same stale-
// height bug the comment below documents, which is the argument for one copy.
import { useCallback, useEffect, useLayoutEffect, useRef } from "react";

/** How many lines a composer grows to before it becomes a scroller. Ten is
 *  about as far as a prompt can push Run (or Build) and the result down before
 *  they leave the fold, and text longer than that is being pasted, not read
 *  back. Mirrored by a max-height utility on each composer's own textarea —
 *  `composerTextareaClass` in apps/ai_models/playground/controls.tsx and the
 *  home hero's input. Those utilities and this cap have to name the same
 *  number of lines, or the smaller of the two wins and the other is dead. */
export const COMPOSER_MAX_LINES = 10;

/** A composer textarea that grows with its own text, so a Shift+Enter newline
 *  is visible and a wrapped prompt is read whole rather than through a
 *  scroller. Hand it the value the textarea is showing; it returns the ref to
 *  put on the box, and a `grow` for anywhere the height has to be re-measured
 *  without the value moving.
 *
 *  Keyed on the VALUE rather than driven from `onChange`, because a prompt
 *  arrives in these boxes several ways: typed, seeded from the `prompt` URL
 *  param on first render, handed over from an app card, or dropped in by a
 *  starter chip. Only the first goes through onChange, and the others are
 *  exactly the long prompts — the box opened three lines tall with the rest
 *  scrolled away.
 *
 *  The cap is in LINES, not px: the box's font comes from the sheet, and a px
 *  budget silently becomes a different number of lines the moment that font
 *  moves. Measured off the box's own computed line-height on every grow.
 *
 *  The height it writes is an inline px value, which means it goes STALE the
 *  moment the box's width changes and the same text rewraps to more lines: the
 *  box keeps its old height and quietly turns into a scroller. A Clear button
 *  appearing beside the prompt used to be enough to do it; so is a sidebar
 *  toggle or a window resize. Hence the observer — and it watches the WIDTH
 *  only, because reacting to a height change would loop on the very thing this
 *  callback does.
 *
 *  CSS `field-sizing: content` would delete this hook outright. Deliberately
 *  not used: it is Chromium-only, and these sheets are not Chromium-only
 *  sheets. Revisit when Safari and Firefox ship it. */
export function useAutoGrow(value: string, maxLines = COMPOSER_MAX_LINES) {
  const ref = useRef<HTMLTextAreaElement | null>(null);
  const grow = useCallback(() => {
    const box = ref.current;
    if (!box) return;
    const style = getComputedStyle(box);
    // `normal` is not a length, so parseFloat gives NaN there — 1.5 is the
    // line-height these sheets give every composer.
    const line = parseFloat(style.lineHeight) || parseFloat(style.fontSize) * 1.5;
    // scrollHeight counts padding but not border, and box-sizing is border-box
    // globally, so the height written back has to carry both for the cap to be
    // `maxLines` WHOLE lines. Neither composer's textarea has a border today; a
    // stage that gives one to its own should still stop where it says it does.
    const chrome =
      parseFloat(style.paddingTop) +
      parseFloat(style.paddingBottom) +
      parseFloat(style.borderTopWidth) +
      parseFloat(style.borderBottomWidth);
    box.style.height = "auto";
    box.style.height =
      Math.min(box.scrollHeight, Math.round(line * maxLines + chrome)) + "px";
  }, [maxLines]);
  // Layout effect, not effect: this runs between React writing the value and
  // the browser painting, so the box is never on screen at the wrong height.
  useLayoutEffect(grow, [grow, value]);
  useEffect(() => {
    const box = ref.current;
    if (!box || typeof ResizeObserver === "undefined") return;
    let last = box.clientWidth;
    const observer = new ResizeObserver(() => {
      if (box.clientWidth === last) return;
      last = box.clientWidth;
      grow();
    });
    observer.observe(box);
    return () => observer.disconnect();
  }, [grow]);
  return { ref, grow };
}
