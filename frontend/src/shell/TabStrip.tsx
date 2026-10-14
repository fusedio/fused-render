// The shell-tabs strip (shell/TabHost.tsx): one button per tab in STRIP
// order, a close × on each, and a + that opens Home. Presentational — every
// decision (what activating, closing or reordering means for history, the
// URL mirror and the frames) is the host's; this only reports gestures.
//
// The strip is the one place the tab ORDER is visible, and it is the only
// thing a reorder touches: the frames below render in their own stable mount
// order and never move (Tabs.tsx TM-5 — a moved iframe reloads), so a drag
// here is a pure re-sort of buttons.
import { useRef, useState, type PointerEvent as ReactPointerEvent } from "react";

export interface StripTab {
  id: string;
  label: string;
  active: boolean;
}

// How far a press must travel before it is a drag rather than a click. Small
// enough to feel immediate, large enough that a click with a shaky hand still
// selects.
const DRAG_THRESHOLD_PX = 4;

export default function TabStrip({
  tabs,
  onSelect,
  onClose,
  onAdd,
  onReorder,
  onReorderEnd,
}: {
  tabs: StripTab[];
  onSelect: (id: string) => void;
  onClose: (id: string) => void;
  onAdd: () => void;
  /** Live, while dragging: move tab `id` to strip index `to`. */
  onReorder: (id: string, to: number) => void;
  /** The drag let go — the host records the final order once. */
  onReorderEnd: () => void;
}) {
  const stripRef = useRef<HTMLDivElement | null>(null);
  // The press in flight. A ref, not state: pointermove fires far faster than
  // a render needs to, and nothing on screen depends on the raw numbers.
  const pressRef = useRef<{ id: string; x: number; pointer: number; dragging: boolean } | null>(null);
  // Only for the class that lifts the dragged tab.
  const [draggingId, setDraggingId] = useState<string | null>(null);
  // A drag ends in a `click` on the tab it started on; that click must not
  // also select it (a reader dragging a background tab aside is not asking to
  // switch to it).
  const swallowClickRef = useRef(false);

  // The strip index the pointer is over, read off the buttons' live rects —
  // their midpoints, so a tab swaps places once the pointer passes half of
  // its neighbour, the way every tab strip does it.
  const indexAt = (clientX: number): number => {
    const strip = stripRef.current;
    if (!strip) return -1;
    const els = Array.from(strip.querySelectorAll<HTMLElement>("[data-tab-id]"));
    let idx = els.length - 1;
    for (let i = 0; i < els.length; i++) {
      const r = els[i].getBoundingClientRect();
      if (clientX < r.left + r.width / 2) {
        idx = i;
        break;
      }
    }
    return idx;
  };

  const onPointerDown = (e: ReactPointerEvent<HTMLDivElement>, id: string) => {
    if (e.button !== 0) return;
    // A drag released off the strip never got its click, so the flag from it
    // is still up — a fresh press starts clean.
    swallowClickRef.current = false;
    pressRef.current = { id, x: e.clientX, pointer: e.pointerId, dragging: false };
  };

  const onPointerMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    const p = pressRef.current;
    if (!p || p.pointer !== e.pointerId) return;
    if (!p.dragging) {
      if (Math.abs(e.clientX - p.x) < DRAG_THRESHOLD_PX) return;
      p.dragging = true;
      setDraggingId(p.id);
      // Capture so the drag keeps tracking past the strip's edges and over
      // the tab iframes below, which would otherwise eat the pointer events.
      // On the STRIP, not the tab: the live re-sort moves the dragged tab's
      // node (insertBefore), and a moved node loses its capture mid-drag.
      try {
        stripRef.current?.setPointerCapture(e.pointerId);
      } catch {
        /* pointer already gone */
      }
    }
    const to = indexAt(e.clientX);
    const from = tabs.findIndex((t) => t.id === p.id);
    if (to !== -1 && from !== -1 && to !== from) onReorder(p.id, to);
  };

  const endPress = (e: ReactPointerEvent<HTMLDivElement>) => {
    const p = pressRef.current;
    if (!p || p.pointer !== e.pointerId) return;
    pressRef.current = null;
    if (!p.dragging) return;
    swallowClickRef.current = true;
    setDraggingId(null);
    onReorderEnd();
  };

  return (
    <div
      className="shell-tabs-strip"
      role="tablist"
      ref={stripRef}
      // The press starts on a tab; everything after it is the strip's, where
      // the capture lives. A capture lost any other way (the window blurred,
      // the pointer went away) ends the drag where it stands.
      onPointerMove={onPointerMove}
      onPointerUp={endPress}
      onPointerCancel={endPress}
      onLostPointerCapture={endPress}
    >
      {tabs.map((t) => (
        <div
          key={t.id}
          data-tab-id={t.id}
          role="tab"
          aria-selected={t.active}
          tabIndex={t.active ? 0 : -1}
          title={t.label}
          className={
            "shell-tab" + (t.active ? " is-active" : "") + (draggingId === t.id ? " is-dragging" : "")
          }
          onPointerDown={(e) => onPointerDown(e, t.id)}
          onClick={() => {
            if (swallowClickRef.current) {
              swallowClickRef.current = false;
              return;
            }
            onSelect(t.id);
          }}
          // Middle-click closes, as in every browser. On mousedown too, only
          // to stop the platform's autoscroll cursor from starting.
          onMouseDown={(e) => {
            if (e.button === 1) e.preventDefault();
          }}
          onAuxClick={(e) => {
            if (e.button !== 1) return;
            e.preventDefault();
            onClose(t.id);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              onSelect(t.id);
            }
          }}
        >
          <span className="shell-tab-label">{t.label}</span>
          <button
            type="button"
            className="shell-tab-close"
            aria-label={`Close ${t.label}`}
            title="Close tab"
            // Not a drag handle: a press here must not start one.
            onPointerDown={(e) => e.stopPropagation()}
            onClick={(e) => {
              e.stopPropagation();
              onClose(t.id);
            }}
          >
            ×
          </button>
        </div>
      ))}
      <button type="button" className="shell-tab-add" title="New tab" aria-label="New tab" onClick={onAdd}>
        +
      </button>
    </div>
  );
}
