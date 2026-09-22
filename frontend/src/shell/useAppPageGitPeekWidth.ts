// THE GIT PEEK'S WIDTH, measured and dragged exactly the way the file
// preview's companion column always has — same floors, same clamp — but kept
// LOCAL to this page rather than routed through `apps/explorer/lib/side-store`
// (the module-level width the explorer's own companion column and the
// listing's preview pane already share).
//
// Reusing that shared store here would make dragging the app page's git peek
// silently resize the NEXT file preview the reader opens in the explorer, and
// the reverse — two surfaces that have never appeared on screen together
// fighting over one remembered number. `apps/explorer/lib/side-width.ts`'s
// arithmetic (the floors, the default share, the resize clamp) is pure and
// carries no such coupling, so it is reused directly; only the STORAGE is
// deliberately not.
//
// No persistence at all, by the same token: `PEEK_WIDTH_KEY`
// (shell/task-peek-store.ts) is the Tasks peek's own remembered width, not a
// slot this page may borrow. A dragged width here lives for the mount and
// resets on the next open — the simplest thing that does not reach into
// state another feature owns.
import {
  type PointerEvent as ReactPointerEvent,
  type RefObject,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { clampSideWidth, defaultSideWidth } from "@apps/explorer/lib/side-width";

export interface AppPageGitPeekWidth {
  /** The panel's current width, already clamped to what the split can hold. */
  width: number;
  /** Wire onto the seam's `onPointerDown`. */
  onSeamPointerDown: (e: ReactPointerEvent<HTMLElement>) => void;
  /** True for the duration of a drag — key an `iframe { pointer-events: none }`
   *  rule off it (app-page.css), the same "both sides go inert" rule the
   *  explorer's own companion column states over `.stat-split`, because the
   *  git template's iframe and the Overview's opposite it would otherwise
   *  swallow the captured pointer stream mid-drag. */
  dragging: boolean;
}

/** `splitRef` is the `.app-page-split` row both the frame and the peek share —
 *  read for its width, never written to. */
export function useAppPageGitPeekWidth(
  splitRef: RefObject<HTMLElement | null>,
): AppPageGitPeekWidth {
  const [hostW, setHostW] = useState(0);
  // The reader's own drag, or null for "no choice yet — use the split's
  // share", exactly `defaultSideWidth`'s own contract.
  const [chosen, setChosen] = useState<number | null>(null);

  useEffect(() => {
    const el = splitRef.current;
    if (!el) return;
    setHostW(el.clientWidth);
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width;
      if (w !== undefined && w > 0) setHostW(w);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [splitRef]);

  const width = clampSideWidth(chosen ?? defaultSideWidth(hostW), chosen, hostW);

  // Delta from where the pointer went down, not an absolute readout of it —
  // the panel is anchored to the split's own right edge, and a delta is right
  // regardless of where in the window that edge happens to sit.
  const drag = useRef<{ x: number; w: number } | null>(null);
  const [dragging, setDragging] = useState(false);

  const onSeamPointerMove = useCallback(
    (e: PointerEvent) => {
      const start = drag.current;
      if (!start) return;
      // The panel is on the RIGHT: dragging the pointer LEFT (negative delta)
      // widens it.
      const implied = start.w - (e.clientX - start.x);
      setChosen(clampSideWidth(implied, implied, hostW));
    },
    [hostW],
  );
  const onSeamPointerUp = useCallback(() => {
    drag.current = null;
    setDragging(false);
    window.removeEventListener("pointermove", onSeamPointerMove);
    window.removeEventListener("pointerup", onSeamPointerUp);
    window.removeEventListener("pointercancel", onSeamPointerUp);
  }, [onSeamPointerMove]);

  const onSeamPointerDown = useCallback(
    (e: ReactPointerEvent<HTMLElement>) => {
      drag.current = { x: e.clientX, w: width };
      setDragging(true);
      window.addEventListener("pointermove", onSeamPointerMove);
      window.addEventListener("pointerup", onSeamPointerUp);
      window.addEventListener("pointercancel", onSeamPointerUp);
    },
    [width, onSeamPointerMove, onSeamPointerUp],
  );

  return { width, onSeamPointerDown, dragging };
}
