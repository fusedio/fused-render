// The shell half of the preview pane's focus contract (listing/frame-focus.ts
// states it): keep the keyboard on the listing when a preview mounts.
//
// runtime.js asks every rendered page not to take focus, which is the half that
// covers templates nobody has written yet. This is the door guard behind it,
// and it is not belt-and-braces so much as the load-bearing half, for two
// reasons a browser makes plain:
//
//   • `autofocus` is applied BY THE BROWSER, from a candidate queued when the
//     element is inserted — removing the attribute afterwards does not dequeue
//     it, so no amount of cooperation inside the page can reliably beat it;
//   • focus landing on an element INSIDE the frame leaves the parent's
//     activeElement on the <iframe> itself. Blurring the inner element from in
//     there would not give the keyboard back to the shell — only blurring the
//     frame ELEMENT, from out here, does.
//
// Taking it back is that blur, and nothing more: it returns document focus to
// <body>, which is precisely the state the listing's document-level arrow-key
// handler requires (useListingSelection's `navActive`). Focusing a listing row
// instead would be worse — rows are not focusable, and focusing the search
// input would start swallowing the arrows itself.
//
// The pane is SAME-ORIGIN, which is what makes all of this possible: the guard
// listens inside the frame's own document for the two things it cannot learn
// from outside — that the frame took focus, and that the USER is the one who
// reached in. Events do not cross an iframe boundary, so a click inside the
// preview raises nothing in the shell; without listening in there, the guard
// would keep snatching focus back from a user who had deliberately clicked into
// the preview to type.
//
// Scoped to ONE preview: the pane component is keyed on the previewed path, so
// this hook remounts with the frame it guards and "has the user reached into
// the pane" resets when the preview does.
import { useEffect, useRef } from "react";
import { shouldReclaimFocus, tabEntersFrame } from "@apps/explorer/listing/frame-focus";

// How long after a Tab keypress a frame taking focus still counts as the user's
// doing. Tab is deliberate, but it is aimed by the browser rather than by us —
// so it is honoured as a WINDOW rather than latched: tabbing around the shell
// chrome must not leave the pane free to grab focus a second later.
const TAB_GRACE_MS = 300;

// Re-checks after the frame's `load`, for the focus a page takes without the
// parent hearing a focus event — `autofocus` applied at the end of parsing, and
// engines that do not surface an iframe focus to the embedder at all. They also
// re-attempt the inner wiring below, so a document that arrives late is still
// watched. Bounded and few, but they run out to a couple of seconds: a template
// that fetches before it focuses does so well after `load`.
const SETTLE_CHECKS_MS = [0, 60, 250, 800, 1600, 2600];

// What Tab can land on in this document, in order — the input to
// tabEntersFrame, which is where the decision is (this half is the DOM read it
// cannot do). `tabIndex >= 0` is the real test and does most of the work: it is
// already false for a disabled control, for `tabindex="-1"`, and for an anchor
// without an href, so the selector only has to be generous. Elements with no
// client rects are skipped as the browser skips them — that covers `display:
// none` chrome, including the pane that is not there on a narrow window.
//
// Read fresh on the keypress rather than cached: it is one querySelectorAll per
// Tab over a document of chrome, and any cache would be a second, staler
// account of a DOM that changes with every preview.
const FOCUSABLE = [
  "a[href]",
  "button",
  "input",
  "select",
  "textarea",
  "iframe",
  "[tabindex]",
  "[contenteditable=\"true\"]",
].join(",");

function tabStops(): HTMLElement[] {
  return Array.from(document.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    (el) => el.tabIndex >= 0 && el.getClientRects().length > 0
  );
}

export function usePaneFocusGuard<T extends HTMLElement>() {
  const rootRef = useRef<T | null>(null);
  // The user has pressed inside the pane — the shell's half of it, since a
  // press inside the FRAME is only visible from within the frame's document
  // (wired below). From here on the preview owns the keyboard and whatever it
  // does with focus is on the user's behalf.
  const pointerEngaged = useRef(false);
  const lastTabAt = useRef(0);

  useEffect(() => {
    const timers: number[] = [];
    const cleanups: (() => void)[] = [];

    const engaged = () =>
      pointerEngaged.current || Date.now() - lastTabAt.current < TAB_GRACE_MS;

    const reclaim = () => {
      // Read the root through the ref EVERY time, never captured once. The pane
      // renders a different element per state — a skeleton while the target is
      // being stat'ed, then the settled preview — and this effect runs on the
      // FIRST of those. Capturing the node it found there meant capturing the
      // skeleton's div, or null: the guard silently never installed, and the
      // runtime's focus bounce was carrying the whole contract on its own.
      const root = rootRef.current;
      if (!root) return;
      const active = document.activeElement;
      const inFrame = active instanceof HTMLIFrameElement && root.contains(active);
      if (!shouldReclaimFocus(inFrame, engaged())) return;
      const frameEl = active as HTMLIFrameElement;
      // Clear whatever holds focus INSIDE the frame first, so the page is not
      // left with a live caret in a composer the reader never put it in, and so
      // a page that re-asserts focus has nothing to re-assert from.
      try {
        const innerActive = frameEl.contentDocument?.activeElement;
        if (innerActive instanceof HTMLElement) innerActive.blur();
      } catch {
        /* cross-origin: the outer half below is all we get */
      }
      frameEl.blur();
      // …and blurring the frame is not enough on its own. WebKit treats
      // `iframe.blur()` as a no-op: the embedder's activeElement stays on the
      // frame, so the keystrokes keep going there and the listing's arrows stay
      // dead — the exact bug this guard exists to prevent, surviving the guard.
      // Focus has to be moved TO something, so it goes where the keyboard
      // belongs: the listing's search box, which is a state the listing already
      // treats as its own (useListingSelection's `navActive` drives the arrows
      // from there deliberately, since a single-line input has no use for them).
      if (document.activeElement !== frameEl) return;
      const search = root
        .closest(".listing-split")
        ?.querySelector<HTMLInputElement>(".listing-search-input");
      search?.focus();
    };

    // Tell the page it may keep focus: the reader has done something deliberate
    // that the PAGE cannot see. Tab is the case — it is pressed in this
    // document, so the frame never receives the keydown that would lift its own
    // suppression, and its focusin bounce would throw away the focus the user
    // just aimed at it (runtime.js). Nothing happens for a page that never
    // installed the hook.
    const releaseFrameSuppression = (frameEl: HTMLIFrameElement) => {
      try {
        (frameEl?.contentWindow as { __fusedReleaseNoFocus?: () => void } | null)
          ?.__fusedReleaseNoFocus?.();
      } catch {
        /* cross-origin: it has no suppression of ours to lift */
      }
    };

    // A focus move inside the frame is reported to us BEFORE the parent's
    // activeElement settles on the iframe in some engines, so the reclaim is
    // deferred a tick rather than run inline.
    const reclaimSoon = () => timers.push(window.setTimeout(reclaim, 0));

    const onFocusIn = () => reclaim();
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== "Tab") return;
      // The GRACE is a 300ms window and stays unconditional: it only lets the
      // guard stand down for as long as a Tab could plausibly still be landing,
      // and it heals itself.
      lastTabAt.current = Date.now();
      // The RELEASE does not heal. runtime.js's `release` is one-shot and
      // permanent, so this is only for the Tab that is actually about to enter
      // the frame — see tabEntersFrame. It used to fire on every Tab anywhere
      // in the shell, which meant cycling between the search box and a
      // breadcrumb quietly retired the runtime's half of the focus contract for
      // the mounted preview.
      //
      // Before the focus moves, not after: the frame's bounce fires the instant
      // focus lands in it, so lifting the suppression afterwards would already
      // have cost the user the tab stop they aimed at. That is what makes this
      // a PREDICTION about where Tab is going rather than a report of where it
      // went.
      const frameEl = rootRef.current?.querySelector("iframe");
      if (!frameEl) return;
      const active = document.activeElement;
      if (tabEntersFrame(tabStops(), active instanceof HTMLElement ? active : null,
                         frameEl, e.shiftKey)) {
        releaseFrameSuppression(frameEl);
      }
    };
    window.addEventListener("focusin", onFocusIn);
    window.addEventListener("keydown", onKeyDown, true);
    cleanups.push(() => {
      window.removeEventListener("focusin", onFocusIn);
      window.removeEventListener("keydown", onKeyDown, true);
    });

    // The frame is looked up on every attempt, for the same reason as the root:
    // it does not exist yet on the commit this effect runs on. `watched` keeps
    // the wiring idempotent across the repeated attempts below.
    const watchedFrames = new WeakSet<HTMLIFrameElement>();
    const watchedDocs = new WeakSet<Document>();

    // Same-origin, so the frame's own document is readable. This is where the
    // two facts the shell cannot otherwise learn come from: that focus landed
    // inside, and that the USER is the one who reached in. Events do not cross
    // an iframe boundary, so without listening in there a click into the
    // preview raises nothing out here and the guard would keep snatching focus
    // back from a reader who deliberately reached in.
    const watchInside = () => {
      const frame = rootRef.current?.querySelector("iframe");
      if (!frame) return;
      if (!watchedFrames.has(frame)) {
        watchedFrames.add(frame);
        // The frame element taking focus, straight from the element: `focusin`
        // on the window does not report it everywhere.
        frame.addEventListener("focus", reclaimSoon);
        // BOTH listeners are handed to `cleanups`, and the `load` one is not
        // the afterthought it looks like. It is the one that can fire after the
        // effect has been torn down — a slow preview whose frame is replaced by
        // a fast row switch — and what it runs is `watchInside()` + `settle()`,
        // which push six fresh timers into the already-drained `timers` array
        // and fresh document listeners into the already-drained `cleanups`. Not
        // one of them would ever be cleared, so the leak accrued once per
        // preview switch, on the frames that load slowest.
        const onLoad = () => {
          watchInside();
          settle();
        };
        frame.addEventListener("load", onLoad);
        cleanups.push(() => {
          frame.removeEventListener("focus", reclaimSoon);
          frame.removeEventListener("load", onLoad);
        });
      }
      let doc: Document | null = null;
      try {
        doc = frame.contentDocument;
      } catch {
        return; // not same-origin after all — the outer listeners still hold
      }
      // A fresh iframe starts on an about:blank that the real page REPLACES, so
      // listeners attached too early go with it; each document is wired once,
      // whenever it turns up.
      if (!doc || watchedDocs.has(doc)) return;
      watchedDocs.add(doc);
      const seen = doc;
      // POINTER only. A keydown reaching this document is not evidence the
      // reader chose the preview — it is evidence focus leaked into it, and
      // counting it stood the guard down permanently on the very keystroke it
      // was meant to rescue. A click is unambiguous; a keyboard user's
      // deliberate route in is Tab, which the shell sees for itself.
      const engage = () => {
        pointerEngaged.current = true;
      };
      seen.addEventListener("pointerdown", engage, true);
      seen.addEventListener("focusin", reclaimSoon, true);
      cleanups.push(() => {
        seen.removeEventListener("pointerdown", engage, true);
        seen.removeEventListener("focusin", reclaimSoon, true);
      });
    };

    // Each tick re-checks focus AND re-attempts the wiring, which is what makes
    // the whole thing survive a frame that does not exist yet on this commit.
    function settle() {
      SETTLE_CHECKS_MS.forEach((ms) =>
        timers.push(
          window.setTimeout(() => {
            watchInside();
            reclaim();
          }, ms)
        )
      );
    }
    settle();

    return () => {
      timers.forEach(clearTimeout);
      cleanups.forEach((fn) => fn());
    };
  }, []);

  // Spread onto the pane's root element: presses on the pane's own CHROME (its
  // header, its mode menu) are engagement too, and those the shell does see.
  // Capture phase, so a press anywhere in the pane counts even if something
  // inside stops the event.
  return {
    rootRef,
    guardProps: {
      onPointerDownCapture: () => {
        pointerEngaged.current = true;
      },
    },
  };
}
