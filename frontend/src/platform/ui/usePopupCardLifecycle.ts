// The floating popup column's shared lifecycle — extracted from
// `JobPopupCard.tsx` (SPEC-toasts-become-notifications.md §2) so a second
// card kind (a client-raised message, `MessagePopupCard.tsx`) can reuse it
// wholesale instead of re-implementing it by hand. FOUR behaviours live
// here, each found the hard way in `JobPopupCard.tsx` originally — a second
// hand-rolled copy would be very likely to get at least one of them wrong
// again, starting with the iframe-blur edge case:
//
//   1. MOUNT-ONCE TIMER — after `visibleMs`, start the exit (`leaving`).
//      `visibleMs: null` skips this entirely: the card only ever leaves by a
//      manual trigger (outside press, its own ✕). This is what
//      `IS_TOP_EMBED` needs for an "attention" message (SPEC §4): a tab or
//      bookmark opened standalone has no shell to retain the message for it,
//      so an auto-expiring popup would show an error for 2.5s and then lose
//      it with no history anywhere.
//   2. EXIT TIMER — once `leaving`, call `onGone` after `TOAST_EXIT_MS`.
//   3. OUTSIDE-PRESS DISMISSAL — a `click` anywhere that is not this card's
//      own DOM node and not elsewhere inside `.notif-host` starts the same
//      exit. `click`, not `pointerdown`, and no `preventDefault`/
//      `stopPropagation` — see the original comment in `JobPopupCard.tsx`'s
//      git history for why a `pointerdown`-based collapse stole a sibling
//      toast's own action-button click.
//   4. IFRAME-BLUR EDGE CASE — app pages are hosted in iframes, so a press
//      inside one never fires a `click` this document can see at all. Only
//      the EDGE (activeElement was not an iframe, now is) dismisses; a
//      window blur while an iframe already held focus (alt-tab, devtools, a
//      native file picker) must not.
//
// `globalThis`, not `window`/`document`, for every timer and listener here —
// the same reason `lib/notifications.ts` (and `lib/toast.ts` before it)
// gives at length: `window`/`document` are no-op stubs in the test DOM shim,
// while `globalThis` is a real `EventTarget` in Bun, so a listener attached
// here is one a test can actually exercise, and a timer scheduled through it
// cannot abort an unrelated bun test file that has installed no DOM shim.
import { useEffect, useRef, type RefObject } from "react";

export function usePopupCardLifecycle({
  cardRef,
  leaving,
  setLeaving,
  onGone,
  visibleMs,
  exitMs,
}: {
  cardRef: RefObject<HTMLElement | null>;
  leaving: boolean;
  setLeaving: (leaving: boolean) => void;
  onGone: () => void;
  /** How long the card stays fully visible before its exit starts. `null`
   *  means "never on its own" — see behaviour 1 above. */
  visibleMs: number | null;
  exitMs: number;
}): void {
  // Read by the exit timer without re-arming it on every render — a fresh
  // `onGone` closure from the parent's own re-render must not restart this
  // card's countdown.
  const goneRef = useRef(onGone);
  goneRef.current = onGone;

  // Runs exactly once per card's whole life: every caller mounts this with a
  // `key` unique to the event it is showing (job id + finished_at; a
  // message's own notification id), so a NEW event is always a fresh
  // instance rather than this effect re-running mid-flight for the same one.
  useEffect(() => {
    if (visibleMs === null) return;
    const t = globalThis.setTimeout(() => setLeaving(true), visibleMs);
    return () => globalThis.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!leaving) return;
    const t = globalThis.setTimeout(() => goneRef.current(), exitMs);
    return () => globalThis.clearTimeout(t);
  }, [leaving, exitMs]);

  const cardRefLive = cardRef;
  useEffect(() => {
    if (leaving) return;
    const isInside = (target: Node | null) => {
      if (!target) return false;
      if (cardRefLive.current?.contains(target)) return true;
      const el = target as Node & { closest?: (sel: string) => Element | null };
      return !!el.closest?.(".notif-host");
    };
    const onOutside = (e: Event) => {
      if (isInside(e.target as Node | null)) return;
      setLeaving(true);
    };
    globalThis.addEventListener("click", onOutside, true);
    return () => globalThis.removeEventListener("click", onOutside, true);
  }, [leaving, cardRefLive, setLeaving]);

  const wasIframeRef = useRef(document.activeElement instanceof HTMLIFrameElement);
  useEffect(() => {
    if (leaving) return;
    const onBlur = () => {
      const isIframeNow = document.activeElement instanceof HTMLIFrameElement;
      if (isIframeNow && !wasIframeRef.current) setLeaving(true);
      wasIframeRef.current = isIframeNow;
    };
    globalThis.addEventListener("blur", onBlur);
    return () => globalThis.removeEventListener("blur", onBlur);
  }, [leaving, setLeaving]);
}
