// A textarea that grows with its own text. Lives in platform because two apps
// need it — the Playground's stage composers (ai_models) and the app-building
// composer in the Home/apps hero (builder) — and an app may only import
// platform + itself. It used to be a hook inside the Playground's controls.tsx;
// the builder composer grew the same feature and reintroduced the same stale-
// height bug the comment below documents, which is the argument for one copy.
import { useCallback, useEffect, useRef } from "react";

/** A composer textarea that grows with its own text, so a Shift+Enter newline
 *  is visible. Returns the ref to hand the textarea and the `grow` to call on
 *  change (or from an effect on the value, when the text can also be set
 *  programmatically — a starter chip landing a brief).
 *
 *  The height it writes is an inline px value, which means it goes STALE the
 *  moment the box's width changes and the same text rewraps to more lines: the
 *  box keeps its old height and quietly turns into a scroller. A Clear button
 *  appearing beside the prompt used to be enough to do it; so is a sidebar
 *  toggle or a window resize. Hence the observer — and it watches the WIDTH
 *  only, because reacting to a height change would loop on the very thing this
 *  callback does.
 *
 *  `max` caps the height in px. Pass `Infinity` when the ceiling is a CSS
 *  `max-height` on the element instead, which is the same clamp expressed in
 *  rows rather than pixels.
 *
 *  CSS `field-sizing: content` would delete this hook outright. Deliberately
 *  not used: it is Chromium-only, and this app is not Chromium-only. Revisit
 *  when Safari and Firefox ship it. */
export function useAutoGrow(max = 180) {
  const ref = useRef<HTMLTextAreaElement | null>(null);
  const grow = useCallback(() => {
    const box = ref.current;
    if (!box) return;
    box.style.height = "auto";
    box.style.height = Math.min(box.scrollHeight, max) + "px";
  }, [max]);
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
