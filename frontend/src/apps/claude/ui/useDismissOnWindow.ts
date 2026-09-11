// THE TWO DISMISSALS BASE UI DOES NOT COVER: window `blur` and window `resize`.
//
// Base UI's Popover and Menu answer outside-press and Escape, which is the
// right contract for a popover in an ordinary page. This chat is not one: it
// frames a document it does not own, and the layout under an open menu moves on
// a measured ladder. So T binds two more listeners, and both have a case behind
// them rather than being belt-and-braces.
//
//   * `blur` (T:12146, T:12602) — for the one thing outside-press cannot see:
//     THE CLICK LANDED INSIDE THE PREVIEW IFRAME. That press never reaches this
//     document, so no outside-press fires, and the menu was left hanging over a
//     pane the reader had already moved on to. Worse for the Schedule confirm,
//     whose Continue NAVIGATES AWAY from the conversation: an orphaned confirm
//     floating over a pane the reader has since clicked into is one keypress
//     from leaving the chat.
//   * `resize` (T:12149, T:12605), with T's reason: "nothing repositions an open
//     menu — a scroll or resize under it would leave it pointing at a pill that
//     has moved, so it goes away instead." Base UI DOES reposition, which is
//     arguably better — except that the pill's own WIDTH changes on the fit
//     ladder (`applyFit` → `fitSelect`), so a repositioned menu can end up
//     floored to a width its anchor no longer has.
//
// ONE hook for both events and every caller, because the three popovers that
// want it (the model/effort/permission pills, the Schedule seat, and its
// confirm) had this as three chances to disagree — and `ui/SchedConfirm.tsx`'s
// own header already documents "outside press, Escape, window blur" as the
// intended contract, so this was stated intent with no implementation behind
// it rather than a design change.
import { useEffect } from "react";

export interface DismissOnWindowOptions {
  /** Bind `resize` as well as `blur`. Default true — T binds both at both
   *  sites; a caller whose popover genuinely does follow its anchor can opt
   *  out and say so. */
  resize?: boolean;
  /** Injected in tests. Defaults to `window`. */
  target?: Pick<Window, "addEventListener" | "removeEventListener"> | null;
}

/**
 * While `open`, dismiss on window `blur` and window `resize`.
 *
 * BOUND ONLY WHILE OPEN, which matters for more than tidiness: `resize` fires
 * on every frame of a window drag and on every keyboard-driven viewport change,
 * and a closed popover has no business waking for either.
 *
 * `close` is read through the effect's own closure and the effect re-binds when
 * it changes, so a caller need not memoise it — but `open` is what gates the
 * work, so a caller that re-creates `close` every render pays only a
 * remove/add pair and never a stray listener.
 */
export function useDismissOnWindow(
  open: boolean,
  close: () => void,
  opts: DismissOnWindowOptions = {},
): void {
  const { resize = true, target } = opts;
  useEffect(() => {
    if (!open) return;
    const view =
      target ?? (typeof window !== "undefined" ? window : null);
    if (!view || typeof view.addEventListener !== "function") return;
    const onDismiss = (): void => close();
    view.addEventListener("blur", onDismiss);
    if (resize) view.addEventListener("resize", onDismiss);
    return () => {
      view.removeEventListener("blur", onDismiss);
      if (resize) view.removeEventListener("resize", onDismiss);
    };
  }, [open, close, resize, target]);
}

export default useDismissOnWindow;
