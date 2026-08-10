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
import { shouldReclaimFocus } from "@apps/explorer/listing/frame-focus";

// How long after a Tab keypress a frame taking focus still counts as the user's
// doing. Tab is deliberate, but it is aimed by the browser rather than by us —
// so it is honoured as a WINDOW rather than latched: tabbing around the shell
// chrome must not leave the pane free to grab focus a second later.
const TAB_GRACE_MS = 300;

// Re-checks after the frame's `load`, for the focus a page takes without the
// parent hearing a focus event — `autofocus` applied at the end of parsing, and
// engines that do not surface an iframe focus to the embedder at all. Bounded
// and few: a preview that has not tried to take focus within a second of
// loading is not going to.
const SETTLE_CHECKS_MS = [0, 60, 250, 800];

export function usePaneFocusGuard<T extends HTMLElement>() {
  const rootRef = useRef<T | null>(null);
  // The user has pressed inside the pane — the shell's half of it, since a
  // press inside the FRAME is only visible from within the frame's document
  // (wired below). From here on the preview owns the keyboard and whatever it
  // does with focus is on the user's behalf.
  const pointerEngaged = useRef(false);
  const lastTabAt = useRef(0);

  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const timers: number[] = [];
    const cleanups: (() => void)[] = [];

    const engaged = () =>
      pointerEngaged.current || Date.now() - lastTabAt.current < TAB_GRACE_MS;

    const reclaim = () => {
      const active = document.activeElement;
      const inFrame = active instanceof HTMLIFrameElement && root.contains(active);
      if (!shouldReclaimFocus(inFrame, engaged())) return;
      (active as HTMLIFrameElement).blur();
    };

    // A focus move inside the frame is reported to us BEFORE the parent's
    // activeElement settles on the iframe in some engines, so the reclaim is
    // deferred a tick rather than run inline.
    const reclaimSoon = () => timers.push(window.setTimeout(reclaim, 0));

    const onFocusIn = () => reclaim();
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Tab") lastTabAt.current = Date.now();
    };
    window.addEventListener("focusin", onFocusIn);
    window.addEventListener("keydown", onKeyDown, true);
    cleanups.push(() => {
      window.removeEventListener("focusin", onFocusIn);
      window.removeEventListener("keydown", onKeyDown, true);
    });

    const frame = root.querySelector("iframe");
    if (frame) {
      // The frame element taking focus, straight from the element: `focusin` on
      // the window does not report it everywhere.
      frame.addEventListener("focus", reclaimSoon);
      cleanups.push(() => frame.removeEventListener("focus", reclaimSoon));

      // Same-origin, so the frame's own document is readable. This is where the
      // two facts the shell cannot otherwise learn come from.
      const watchInside = () => {
        let doc: Document | null = null;
        try {
          doc = frame.contentDocument;
        } catch {
          return; // not same-origin after all — the outer listeners still hold
        }
        if (!doc) return;
        const engage = () => {
          pointerEngaged.current = true;
        };
        doc.addEventListener("pointerdown", engage, true);
        doc.addEventListener("keydown", engage, true);
        doc.addEventListener("focusin", reclaimSoon, true);
        cleanups.push(() => {
          doc.removeEventListener("pointerdown", engage, true);
          doc.removeEventListener("keydown", engage, true);
          doc.removeEventListener("focusin", reclaimSoon, true);
        });
      };

      const onLoad = () => {
        watchInside();
        SETTLE_CHECKS_MS.forEach((ms) => timers.push(window.setTimeout(reclaim, ms)));
      };
      frame.addEventListener("load", onLoad);
      cleanups.push(() => frame.removeEventListener("load", onLoad));
      // A frame that finished loading before this effect ran (a cached
      // template, a re-render) fires no further `load` — watch it now too.
      watchInside();
    }
    SETTLE_CHECKS_MS.forEach((ms) => timers.push(window.setTimeout(reclaim, ms)));

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
