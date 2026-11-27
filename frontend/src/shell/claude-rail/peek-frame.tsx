// A RAIL CARD THAT SHOWS A PAGE IN A FRAME — the mode cards (ModePeek) and
// the left sidebar's cards (shell/left-rail LeftPeek). Lifted out of ModePeek
// (owner, 2026-11-27: the left cards are framed pages too) so the two cannot
// drift; ModePeek's behaviour is unchanged.
//
// THE FRAME STANDS BEHIND A PARAM BOUNDARY (`PeekFrame`): a page's
// `fused.params.set` climbs to the topmost same-origin window and writes ITS
// address (runtime.js `findTarget`, D46) — from in here that is the page the
// card stands over, which would take the card's `view=` and the like into its
// own URL. So the page's frame is put inside a blank frame marked
// `_fusedParamBoundary`, the mark a panel's or a tab's shell wears: the climb
// stops below it, on the page's frame, as it does for a panel pane.
//
// THE FRAME IS ANOTHER DOCUMENT, so the chassis' rules are restated for it
// (`usePeekFrameRules`):
//   - a press inside it pins a hover's preview (`pinOnBlur`: the window's
//     blur with the focus on our frame is the only word of it) — the mode
//     cards; the left cards SHIELD their frame while a preview instead (it
//     takes no pointer until a press inside the card made it temp), so a
//     press there is the card's own;
//   - a temp card goes when the focus moves on into a frame that is not ours
//     (no second blur comes for that) — and, `frameBlurCloses` (the left
//     cards, which no ClaudeRail watches), when a press lands in any frame
//     outside the card (rail-pin-lib `frameBlurCloses`: a temp card goes, a
//     held one stays; a frame in a full modal is that modal's);
//   - `reclaimFocus` (the left cards): a preview's frame that takes the
//     keyboard on its own (a page focusing its box on load) hands it back —
//     nobody pressed in it, the shield saw to that.
import { useEffect, useLayoutEffect, useRef, type MutableRefObject } from "react";
import type { DoorCardStore } from "./door-card-store";
import { inFullModal } from "./rail-pin-lib";

/** How long the frame outlives a close (the card's fade). */
export const FRAME_LINGER_MS = 400;

/** THE CARD'S FRAME (header): a blank frame marked as a param boundary, and
 *  the page's frame on `src` inside it. Keyed by `src` where it is mounted,
 *  so a new address is a new frame. `onLoad` hears the inner frame's every
 *  load. */
export function PeekFrame({
  src,
  title,
  frameRef,
  onLoad,
}: {
  src: string;
  title: string;
  frameRef: MutableRefObject<HTMLIFrameElement | null>;
  onLoad: (inner: HTMLIFrameElement) => void;
}) {
  const onLoadRef = useRef(onLoad);
  onLoadRef.current = onLoad;
  useLayoutEffect(() => {
    const outer = frameRef.current;
    if (!outer) return;
    let inner: HTMLIFrameElement | null = null;
    const build = () => {
      const w = outer.contentWindow;
      const d = outer.contentDocument;
      if (!w || !d?.body) return;
      // Built already, in this very document (an engine that swaps the first
      // blank document for another on load builds twice otherwise).
      if (inner && inner.ownerDocument === d && inner.isConnected) return;
      w._fusedParamBoundary = true;
      d.documentElement.style.cssText = "height:100%;color-scheme:" + getComputedStyle(outer).colorScheme;
      d.body.style.cssText = "margin:0;height:100%;overflow:hidden;background:transparent";
      const el = d.createElement("iframe");
      inner = el;
      el.title = title;
      el.style.cssText = "display:block;border:0;width:100%;height:100%";
      el.addEventListener("load", () => onLoadRef.current(el));
      el.src = src;
      d.body.appendChild(el);
    };
    build();
    outer.addEventListener("load", build);
    return () => outer.removeEventListener("load", build);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [src]);
  return <iframe ref={frameRef} className="claude-rail-mode-frame" title={title} />;
}

/** The frame's rules (header), for the card `store` draws: `up` (shown),
 *  `temp` (pressed, not held). */
export function usePeekFrameRules(o: {
  store: DoorCardStore;
  frameRef: MutableRefObject<HTMLIFrameElement | null>;
  up: boolean;
  temp: boolean;
  /** A blur onto our frame is a press inside: it pins a preview. */
  pinOnBlur: boolean;
  /** A preview's frame that takes the keyboard on its own hands it back. */
  reclaimFocus?: boolean;
  /** A press into a frame outside the card closes a temp card (the window's
   *  blur onto it) — ClaudeRail's own rule for its cards, for a card no
   *  ClaudeRail watches. */
  frameBlurCloses?: boolean;
}): void {
  const { store, frameRef, up, temp, pinOnBlur, reclaimFocus = false, frameBlurCloses = false } = o;
  // A PRESS INSIDE THE FRAME PINS A PREVIEW (header).
  useEffect(() => {
    if (!up || !pinOnBlur) return;
    let tick = 0;
    const onBlur = () => {
      window.clearTimeout(tick);
      tick = window.setTimeout(() => {
        const f = frameRef.current;
        if (!f || document.activeElement !== f || store.state().pinned) return;
        const intent = store.intent();
        intent.pressStart();
        intent.pressEnd();
      }, 0);
    };
    window.addEventListener("blur", onBlur);
    return () => {
      window.clearTimeout(tick);
      window.removeEventListener("blur", onBlur);
    };
  }, [up, store, pinOnBlur, frameRef]);
  // …OR, SHIELDED, A PREVIEW NEVER TAKES THE KEYBOARD ON ITS OWN (header).
  useEffect(() => {
    if (!up || !reclaimFocus) return;
    // The page's own focus, kept up to date while the preview stands — the
    // element the reader was on when the frame took the keyboard, not the
    // one they were on when the card opened (CodeRabbit on #2495).
    let was = document.activeElement;
    const onFocusIn = (e: FocusEvent) => {
      const t = e.target;
      if (t instanceof Element && t !== frameRef.current && !(frameRef.current && t.contains(frameRef.current))) was = t;
    };
    document.addEventListener("focusin", onFocusIn, true);
    let tick = 0;
    const onBlur = () => {
      window.clearTimeout(tick);
      tick = window.setTimeout(() => {
        const f = frameRef.current;
        if (!f || document.activeElement !== f || store.state().pinned) return;
        if (was instanceof HTMLElement && was !== document.body && was.isConnected && !f.contains(was)) was.focus({ preventScroll: true });
        else f.blur();
      }, 0);
    };
    window.addEventListener("blur", onBlur);
    return () => {
      window.clearTimeout(tick);
      window.removeEventListener("blur", onBlur);
      document.removeEventListener("focusin", onFocusIn, true);
    };
  }, [up, store, reclaimFocus, frameRef]);
  // …A PRESS INTO A FRAME OUTSIDE THE CARD CLOSES A TEMP ONE (header).
  useEffect(() => {
    if (!temp || !frameBlurCloses) return;
    let tick = 0;
    const onBlur = () => {
      window.clearTimeout(tick);
      tick = window.setTimeout(() => {
        const a = document.activeElement;
        if (!(a instanceof HTMLIFrameElement) || inFullModal(a)) return;
        const s = store.state();
        if (!s.up || !s.pinned || s.held || document.getElementById(store.id)?.contains(a)) return;
        store.close();
      }, 0);
    };
    window.addEventListener("blur", onBlur);
    return () => {
      window.clearTimeout(tick);
      window.removeEventListener("blur", onBlur);
    };
  }, [temp, store, frameBlurCloses]);
  // …AND A TEMP CARD GOES WHEN THE FOCUS MOVES INTO ANOTHER FRAME (header).
  useEffect(() => {
    if (!temp) return;
    let inOurs = document.activeElement === frameRef.current;
    const t = window.setInterval(() => {
      const a = document.activeElement;
      if (a === frameRef.current) {
        inOurs = true;
        return;
      }
      if (inOurs && a instanceof HTMLIFrameElement && !inFullModal(a) && !document.getElementById(store.id)?.contains(a)) {
        store.close();
        return;
      }
      if (!(a instanceof HTMLIFrameElement)) inOurs = false;
    }, 200);
    return () => window.clearInterval(t);
  }, [temp, store, frameRef]);
}
