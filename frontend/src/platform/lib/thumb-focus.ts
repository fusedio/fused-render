// The SHELL half of the card grids' focus contract (frame-focus.ts states it,
// step 4): a live card thumbnail that grabs focus — or scrolls itself into view
// — must not take the grid with it.
//
// THE SYMPTOM, which is the whole reason this is restoration and not
// prevention. Focusing an element inside a same-origin frame scrolls that frame
// into view, and the scroll walks out of the frame into the embedder's own
// scroller. Card thumbnails mount up to 300px OUTSIDE the viewport
// (preview-start's lookahead), so an app that focuses an input on boot scrolls
// the /apps grid to its own row the instant its card mounts — under a reader
// who is mid-scroll, which is what makes it read as choppy scrolling rather
// than as a jump. `scrollIntoView` from inside the frame does the same thing
// with no focus involved at all.
//
// runtime.js covers the ordinary routes into focus from inside the page and
// nothing here replaces it. This is the guard behind it, and it is deliberately
// blind to WHICH route was taken: it hears that a frame took focus, or the
// frame's own runtime tells it a bounced focus / a scrollIntoView just
// happened, and it puts the scroller back. A new route into focus — the next
// `showModal()`, the next engine quirk — needs nothing added.
//
// Two things it does NOT do, both on purpose:
//
//   * it does not touch the PANE. A pane preview is something the reader can
//     deliberately click and tab into; taking focus off it is a decision with
//     an owner (usePaneFocusGuard) and this has no business in it. Card
//     thumbnails have no deliberate acts at all — a pointer-events shield keeps
//     every click on the card — so a frame in one taking focus is always wrong.
//   * it does not chase focus around. The blur is a courtesy so the keyboard
//     goes back to the document; the scroll restore is the fix.
import { CARD_SCROLLER_SELECTOR } from "./preview-start";
import { displacedScrollers } from "./frame-focus";
import type { ScrollOffset } from "./frame-focus";

// Where each tracked scroller was last seen by the reader — read through the
// set below rather than walked, so a WeakMap.
const remembered = new WeakMap<Element, ScrollOffset>();
// The scrollers themselves, ITERABLE, because a pin arriving from inside a frame
// names no frame and so has no chain to walk up. It holds page-level scroll
// containers — `.apps-page`, `.home-page`, the document scroller — one per
// surface rather than one per card, but a Set is a STRONG reference and those
// containers unmount: Home → /apps → Home would otherwise leave one detached
// scroller (and whatever its subtree still holds) alive per hop. So anything
// disconnected is dropped whenever the set is walked, which is every
// registration and every pin.
const tracked = new Set<Element>();
const shielded = new WeakSet<HTMLIFrameElement>();

function forget(el: Element): void {
  tracked.delete(el);
  remembered.delete(el);
}

function offsetOf(el: Element): ScrollOffset {
  return { top: el.scrollTop, left: el.scrollLeft };
}

function record(el: Element): void {
  remembered.set(el, offsetOf(el));
}

// A scroll EVENT is the reader's value — see the reasoning on displacedScrollers.
// Capture, because scroll does not bubble; passive, because this only reads.
// One listener for the whole shell, installed on first registration rather than
// at import so a bundle that never draws a card pays nothing.
let listening = false;
function listen(): void {
  if (listening || typeof document === "undefined") return;
  listening = true;
  document.addEventListener(
    "scroll",
    (e) => {
      const t = e.target;
      // `document` is the target for the page scroller; its scrollingElement is
      // the element whose offsets actually move.
      const el = t === document ? document.scrollingElement : t instanceof Element ? t : null;
      if (el && tracked.has(el)) record(el);
    },
    { capture: true, passive: true },
  );
}

// Put back whatever moved without the reader. Safe to call at any time: a
// scroller nobody displaced is not in the answer, so a spurious call is a
// handful of offset reads.
export function pinCardScrollers(): void {
  const live: Element[] = [];
  for (const el of tracked) {
    if (el.isConnected) live.push(el);
    else forget(el);
  }
  for (const { el, to } of displacedScrollers(live, (x) => remembered.get(x), offsetOf)) {
    el.scrollTop = to.top;
    el.scrollLeft = to.left;
  }
}

// Register a thumbnail's iframe. A REF CALLBACK, so it is the card's ordinary
// `ref={shieldThumbFrame}` — module-level and therefore stable, which is what
// keeps React from detaching and re-attaching it on every render.
//
// The `focus` listener goes on the element rather than through React's
// onFocus: focus landing INSIDE a frame is reported as a focus event on the
// frame ELEMENT, and delegated focusin does not carry it in every engine (the
// pane guard learned the same thing). Nothing is ever removed — the listener is
// one shared module function, so it dies with the element it is on.
export function shieldThumbFrame(el: HTMLIFrameElement | null): void {
  if (!el || shielded.has(el)) return;
  shielded.add(el);
  listen();
  for (const s of tracked) if (!s.isConnected) forget(s);
  // Recorded NOW, at mount, and not left to the first scroll event: a yank on a
  // page the reader has not scrolled yet — the grid's own first paint, where
  // several cards mount at once — would otherwise have no earlier value to be
  // put back to, and displacedScrollers correctly refuses to invent one.
  const scroller = el.closest(CARD_SCROLLER_SELECTOR);
  for (const s of [scroller, document.scrollingElement]) {
    if (!s) continue;
    tracked.add(s);
    if (!remembered.has(s)) record(s);
  }
  el.addEventListener("focus", onFrameFocus);
}

function onFrameFocus(e: Event): void {
  const frame = e.currentTarget;
  // The scroll is already done by the time this runs — the focusing steps scroll
  // the frame into view and fire the focus events after — so the order is "undo
  // that, then hand the keyboard back".
  pinCardScrollers();
  if (frame instanceof HTMLIFrameElement) {
    try {
      const inner = frame.contentDocument?.activeElement;
      if (inner instanceof HTMLElement) inner.blur();
    } catch {
      /* cross-origin: the outer blur is all there is */
    }
    // A courtesy, not the fix, and knowingly best-effort: WebKit treats
    // `iframe.blur()` as a no-op (the pane guard moves focus somewhere real to
    // get around it, which a grid of links has nowhere to do). Leaving focus on
    // a thumbnail costs nothing the reader can feel — there is no keyboard mode
    // on the grid for it to break — where the scroll it dragged along is the
    // whole bug, and that part is already undone above.
    frame.blur();
  }
}

// The same pin, reachable from INSIDE an embedded page. runtime.js calls it on
// every focus it bounces and after every scrollIntoView it lets through, which
// is what covers the two cases this side cannot see: an engine that never fires
// `focus` on the frame element, and a scroll with no focus in it at all.
// An app-internal global rather than part of `fused`, for the same reason
// `__fusedReleaseNoFocus` is — it is plumbing between the shell and the runtime
// that ships with it, not a documented page API.
declare global {
  interface Window {
    __fusedPinThumbScroll?: () => void;
  }
}

if (typeof window !== "undefined") {
  window.__fusedPinThumbScroll = pinCardScrollers;
}
