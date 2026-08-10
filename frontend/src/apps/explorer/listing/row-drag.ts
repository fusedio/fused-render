// The MOVE-DRAG, driven by pointer events: one module-level gesture, started by
// the listing's press arbiter (useMarquee) and run to completion from here.
//
// WHY THIS IS NOT HTML5 DRAG-AND-DROP ANY MORE. `draggable` is a flag the
// browser reads when the movement BEGINS, not when the button goes down — and a
// press on an unselected row selects it on pointerdown, so the row was always
// selected (and so always `draggable`) by the time the browser looked. Every
// press on an unselected row therefore armed a move-drag before the sweep could
// claim the gesture, which is the bug the user reported as "dragging a row from
// the size/modified column still tries to move it". No rule stated in
// drag-drop.ts can fix that, because the native API does not consult it at the
// moment that matters. Owning the pointer is the fix: the gesture is decided
// ONCE, at pointerdown, from a snapshot of the selection taken before the press
// could change it (drag-drop's pressStartsDrag).
//
// WHAT THE NATIVE API WAS GIVING US, and where each piece went:
//
//   dragover/dragenter/drop targeting   → hit-testing on pointermove with
//                                         document.elementFromPoint, against
//                                         the SAME dropIsValid verdict.
//   the move / no-drop cursor           → body classes (explorer.css), since
//                                         `dropEffect` no longer paints them.
//   setDragImage                        → the .fs-drag-image element, moved by
//                                         this module on every pointermove.
//   surviving a remount mid-drag        → module-level state (this file) and the
//                                         module-level in-flight store, plus a
//                                         DOM protocol (data-fs-drop-*) rather
//                                         than React handlers, so a target the
//                                         spring-load has just re-rendered is
//                                         still a target.
//   Escape / an ended drag              → an Escape key listener and
//                                         pointercancel, wired here.
//
// THE DOM PROTOCOL. A drop target declares itself with attributes, not with
// handlers, and this module never holds a reference to one:
//
//   data-fs-drop-path      the folder entries would move INTO
//   data-fs-drop-dir       "1" | "0" — is that path a directory? Absent means
//                          NOT KNOWN YET (a sidebar bookmark, which only the
//                          server can answer for): treated optimistically as a
//                          folder and probed, exactly as the sidebar's own
//                          handler used to.
//   data-fs-drop-announce  the target is OFF SCREEN, so the move has to say so
//                          (a toast) — the sidebar's case.
//   data-spring-target     a breadcrumb crumb: spring-loaded NAVIGATION, never
//                          a drop target.
//
// EVERY DECISION belongs to drag-drop.ts (pure, tested) and marquee.ts's slop /
// auto-scroll arithmetic (likewise). What is here is wiring, kept thin on
// purpose: a headless test cannot see a drag, so nothing it cannot see is
// allowed to decide anything.
import { statPath } from "@platform/lib/api";
import { pushToast } from "@platform/lib/toast";
import { basename } from "@platform/lib/format";
import { moveEntriesInto } from "@apps/explorer/lib/fs-move";
import { autoScrollStep, passedDragSlop, type Point } from "@apps/explorer/listing/marquee";
import {
  clearFsDrag,
  dragGhostLabel,
  dropIsValid,
  fsDragInFlight,
  startFsDrag,
  type DragSource,
  type DropTarget,
} from "@apps/explorer/listing/drag-drop";

export const DROP_PATH_ATTR = "data-fs-drop-path";
export const DROP_DIR_ATTR = "data-fs-drop-dir";
export const DROP_ANNOUNCE_ATTR = "data-fs-drop-announce";
export const SPRING_ATTR = "data-spring-target";

// The classes a hovered target wears. `drop-into` / `drop-reject` are the same
// two everywhere now — listing row, listing background, sidebar bookmark — so
// what lights up cannot disagree with what moves.
const OK_CLASS = "drop-into";
const NO_CLASS = "drop-reject";
// On <body> for the life of the gesture: the cursor (grabbing / no-drop) and
// the text-selection suppression the native drag used to give for free.
const DRAGGING_CLASS = "fs-dragging";
const REFUSED_CLASS = "fs-drag-refused";

// How far from the cursor the ghost sits, in CSS px. Far enough that it never
// covers the row the pointer is actually pointing at.
const GHOST_OFFSET = 14;

// --- who performs the move ---------------------------------------------------
//
// The listing that OWNS the drop target, which is not always the one the drag
// started in: spring-loading a crumb navigates mid-drag, and the drop then
// lands in a freshly mounted listing that has its own refresh and its own
// re-anchor to do. So movers are registered per SCROLLER element and resolved
// from the target at drop time, with the origin listing as the fallback for a
// target outside any listing (a sidebar bookmark).

export interface MoveOpts {
  // The destination is not on screen, so the move announces itself.
  announce: boolean;
}
export type ListingMover = (paths: string[], dir: string, opts: MoveOpts) => void;

const movers = new WeakMap<HTMLElement, ListingMover>();

export function registerListingMover(scroller: HTMLElement, mover: ListingMover): () => void {
  movers.set(scroller, mover);
  return () => {
    if (movers.get(scroller) === mover) movers.delete(scroller);
  };
}

// --- spring-loaded crumbs ----------------------------------------------------
//
// Breadcrumb keeps the timer, the armed highlight and the disarm rule
// (drag-drop's springDisarms); this module only tells it when the pointer
// enters and leaves a crumb, in the SAME ORDER the DOM's drag events used to —
// enter(new) before leave(old) — because that ordering is what springDisarms is
// written against.

export interface SpringHandler {
  enter: (target: string) => void;
  leave: (target: string) => void;
  // The drag ended (dropped, cancelled, Escape) — the one path every drag
  // reaches, including the one that ends while the cursor is still on a crumb.
  end: () => void;
}

const springs = new Set<SpringHandler>();

export function registerSpring(h: SpringHandler): () => void {
  springs.add(h);
  return () => {
    springs.delete(h);
  };
}

// --- is the target a folder? -------------------------------------------------
//
// Only for targets that don't say (`data-fs-drop-dir` absent — a sidebar
// bookmark, whose path only the server can classify). Probed ONCE per path per
// session and cached. Until it lands the target is treated optimistically as a
// folder — the common case by far, and refusing something we simply haven't
// asked about yet reads as broken — and the DROP itself waits for the real
// answer before moving anything. A path we cannot stat is not one we should be
// moving files into, so a failed probe resolves to "not a folder".
const kindCache = new Map<string, boolean>();
const kindPending = new Set<string>();

function probeKind(path: string): void {
  if (kindCache.has(path) || kindPending.has(path)) return;
  kindPending.add(path);
  void statPath(path)
    .then((s) => kindCache.set(path, s.is_dir))
    .catch(() => kindCache.set(path, false))
    .finally(() => {
      kindPending.delete(path);
      // The probe can easily outlive the hover it was started for; repaint only
      // if the pointer is still on that target and a drag is still in flight.
      if (live?.active && live.spot?.target.path === path) paint(hitTargetOf(live.spot.el));
    });
}

// --- the gesture -------------------------------------------------------------

interface Spot {
  el: HTMLElement;
  target: DropTarget;
  announce: boolean;
}

interface Live {
  pointerId: number;
  origin: Point; // client coords, for the slop test
  clientX: number;
  clientY: number;
  items: DragSource[];
  names: string[];
  // The listing the drag STARTED in — the mover of last resort, and the
  // scroller the edge auto-scroll falls back to.
  originScroller: HTMLElement | null;
  // false until the press has travelled the slop: before that it is still a
  // click, and nothing is shown, stored or captured.
  active: boolean;
  ghost: HTMLElement | null;
  spot: Spot | null;
  spring: string | null;
  scroller: HTMLElement | null; // the scroller under the pointer, for auto-scroll
  raf: number;
}

let live: Live | null = null;

export function isRowDragActive(): boolean {
  return live !== null && live.active;
}

// Start tracking a press that the arbiter has ruled a move-drag. Nothing is
// visible yet — a press that never travels the slop is a click and must cost
// nothing at all.
export function beginRowDrag(press: {
  pointerId: number;
  clientX: number;
  clientY: number;
  items: DragSource[];
  names: string[];
  scroller: HTMLElement | null;
}): void {
  if (live) endDrag();
  if (!press.items.length) return;
  live = {
    pointerId: press.pointerId,
    origin: { x: press.clientX, y: press.clientY },
    clientX: press.clientX,
    clientY: press.clientY,
    items: press.items,
    names: press.names,
    originScroller: press.scroller,
    active: false,
    ghost: null,
    spot: null,
    spring: null,
    scroller: press.scroller,
    raf: 0,
  };
  window.addEventListener("pointermove", onPointerMove);
  window.addEventListener("pointerup", onPointerUp);
  window.addEventListener("pointercancel", onPointerCancel);
  // Capture phase, and stopped there: Escape during a drag cancels the DRAG,
  // and must not also reach the listing's own Escape (which clears the
  // selection — the selection being what is on the move).
  window.addEventListener("keydown", onKeyDown, true);
}

function activate(): void {
  const d = live;
  if (!d) return;
  d.active = true;
  startFsDrag(d.items);
  document.body.classList.add(DRAGGING_CLASS);
  const ghost = document.createElement("div");
  ghost.className = "fs-drag-image";
  ghost.textContent = dragGhostLabel(d.names);
  if (d.names.length > 1) {
    const badge = document.createElement("span");
    badge.className = "fs-drag-count";
    badge.textContent = String(d.names.length);
    ghost.appendChild(badge);
  }
  document.body.appendChild(ghost);
  d.ghost = ghost;
  moveGhost();
  // Pointer capture is set HERE and not at pointerdown, and the difference
  // matters: capturing at the press would route the pointerup to the capture
  // element instead of the row, and the row's own pointerup is what collapses a
  // multi-selection onto a press that never moved (Listing's onRowPointerUp).
  // Captured from the moment it IS a drag, the gesture survives the pointer
  // crossing into the preview pane's iframe — the same reason the divider drag
  // captures (listing/pane.ts). documentElement rather than the pressed row: a
  // spring-loaded navigation unmounts the row mid-drag, and capture on a
  // detached element is capture lost.
  try {
    document.documentElement.setPointerCapture(d.pointerId);
  } catch {
    /* no capture; the window listeners still see the gesture */
  }
}

function moveGhost(): void {
  const d = live;
  if (!d?.ghost) return;
  d.ghost.style.transform = `translate(${d.clientX + GHOST_OFFSET}px, ${d.clientY + GHOST_OFFSET}px)`;
}

// What the element under the pointer offers as a drop target, if anything.
// `closest` walks up, so a row inside the listing wins over the listing
// background it sits in — which is what stopPropagation used to buy.
function hitTargetOf(el: HTMLElement | null): Spot | null {
  const host = el?.closest<HTMLElement>(`[${DROP_PATH_ATTR}]`) ?? null;
  if (!host) return null;
  const path = host.getAttribute(DROP_PATH_ATTR);
  if (!path) return null;
  const dirAttr = host.getAttribute(DROP_DIR_ATTR);
  let isDir: boolean;
  if (dirAttr === "1" || dirAttr === "0") {
    isDir = dirAttr === "1";
  } else {
    // Unknown: optimistic, and asked about.
    probeKind(path);
    isDir = kindCache.get(path) !== false;
  }
  return { el: host, target: { path, isDir }, announce: host.hasAttribute(DROP_ANNOUNCE_ATTR) };
}

function clearSpot(): void {
  const d = live;
  if (!d?.spot) return;
  d.spot.el.classList.remove(OK_CLASS, NO_CLASS);
  d.spot = null;
}

// Paint the affordance for the target under the pointer. The verdict is the
// SAME dropIsValid every other path asks — there is one rule, and the cursor,
// the highlight and the move all read it.
function paint(spot: Spot | null): void {
  const d = live;
  if (!d) return;
  if (d.spot && d.spot.el !== spot?.el) clearSpot();
  if (!spot) {
    document.body.classList.add(REFUSED_CLASS);
    return;
  }
  const ok = dropIsValid(fsDragInFlight(), spot.target).ok;
  d.spot = spot;
  spot.el.classList.toggle(OK_CLASS, ok);
  spot.el.classList.toggle(NO_CLASS, !ok);
  document.body.classList.toggle(REFUSED_CLASS, !ok);
}

// Enter/leave for the crumb under the pointer, in the order springDisarms is
// written against: the NEW crumb is entered before the old one is left.
function springTo(el: HTMLElement | null): void {
  const d = live;
  if (!d) return;
  const target = el?.closest<HTMLElement>(`[${SPRING_ATTR}]`)?.getAttribute(SPRING_ATTR) ?? null;
  if (target === d.spring) return;
  const previous = d.spring;
  d.spring = target;
  if (target !== null) for (const h of springs) h.enter(target);
  if (previous !== null) for (const h of springs) h.leave(previous);
}

// Edge auto-scroll, the same arithmetic and the same feel the sweep has
// (marquee's autoScrollStep). The scroller is re-resolved from the pointer
// every move rather than held: a spring-loaded navigation replaces it mid-drag,
// and the last one seen is the right answer once the pointer has overshot the
// listing's bottom edge entirely.
function scrollLoop(): void {
  const d = live;
  if (!d || !d.active) return;
  const scroller = d.scroller;
  if (!scroller || !scroller.isConnected) {
    d.raf = 0;
    return;
  }
  const r = scroller.getBoundingClientRect();
  const step = d.clientX >= r.left && d.clientX <= r.right
    ? autoScrollStep(d.clientY, { top: r.top, bottom: r.bottom })
    : 0;
  if (step === 0) {
    d.raf = 0;
    return;
  }
  const before = scroller.scrollTop;
  scroller.scrollTop += step;
  // At either end the scroll cannot move; re-hit-testing would be identical
  // work forever, so let the loop idle until the pointer does something.
  if (scroller.scrollTop !== before) repaintAtPointer();
  d.raf = requestAnimationFrame(scrollLoop);
}

function repaintAtPointer(): void {
  const d = live;
  if (!d) return;
  const el = document.elementFromPoint(d.clientX, d.clientY) as HTMLElement | null;
  const scroller = el?.closest<HTMLElement>(".listing-scroll") ?? null;
  if (scroller) d.scroller = scroller;
  paint(hitTargetOf(el));
  springTo(el);
}

function onPointerMove(ev: PointerEvent): void {
  const d = live;
  if (!d || ev.pointerId !== d.pointerId) return;
  d.clientX = ev.clientX;
  d.clientY = ev.clientY;
  if (!d.active) {
    if (!passedDragSlop(d.origin, { x: ev.clientX, y: ev.clientY })) return;
    activate();
  }
  moveGhost();
  repaintAtPointer();
  if (!d.raf) d.raf = requestAnimationFrame(scrollLoop);
}

function onPointerUp(ev: PointerEvent): void {
  const d = live;
  if (!d || ev.pointerId !== d.pointerId) return;
  if (!d.active) {
    // Never left the slop: it was a click, and the press handlers own it.
    endDrag();
    return;
  }
  // Hit-tested at the RELEASE point rather than trusting the last painted
  // target: they are the same target on any ordinary drag, and where the button
  // came up is the honest answer when they are not.
  d.clientX = ev.clientX;
  d.clientY = ev.clientY;
  const spot = hitTargetOf(document.elementFromPoint(ev.clientX, ev.clientY) as HTMLElement | null);
  const dragged = fsDragInFlight();
  // The listing the drag STARTED in, resolved before the gesture is torn down:
  // it is what performs a drop onto a target outside any listing (a sidebar
  // bookmark), because that is the listing whose rows just left.
  const originMover = d.originScroller ? movers.get(d.originScroller) : undefined;
  // Ended BEFORE the move, so nothing can be dropped twice and no highlight
  // outlives the release, whatever the move goes on to do.
  endDrag();
  if (!spot || !dragged.length) return;
  void completeDrop(spot, dragged, originMover);
}

function onPointerCancel(ev: PointerEvent): void {
  if (!live || ev.pointerId !== live.pointerId) return;
  endDrag();
}

// Escape cancels the drag, exactly as it did when the drag was the browser's.
// Handled in the CAPTURE phase and stopped there so the same key does not also
// reach the listing's own Escape, which clears the selection — the selection
// being the thing that was on the move.
function onKeyDown(ev: KeyboardEvent): void {
  if (!live || ev.key !== "Escape") return;
  // Only a drag that is actually under way swallows the key. A press that has
  // not travelled the slop is still a click, and Escape there means what it
  // always means in the listing (clear the selection).
  if (live.active) {
    ev.preventDefault();
    ev.stopPropagation();
  }
  endDrag();
}

// The drop, once the gesture is over. Async only for the one target that has to
// ask the server what it is (a sidebar bookmark whose probe never landed);
// everything else settles synchronously off the cached answer.
async function completeDrop(
  spot: Spot,
  dragged: DragSource[],
  originMover: ListingMover | undefined,
): Promise<void> {
  let target = spot.target;
  if (!spot.el.hasAttribute(DROP_DIR_ATTR)) {
    if (!kindCache.has(target.path)) {
      // Never probed, or the probe is still out: settle it now rather than
      // moving files into something that may be a file.
      const isDir = await statPath(target.path).then(
        (s) => s.is_dir,
        () => false,
      );
      kindCache.set(target.path, isDir);
    }
    target = { path: target.path, isDir: kindCache.get(target.path) === true };
  }
  const verdict = dropIsValid(dragged, target);
  if (!verdict.ok) {
    // The only refusal a user can reach without seeing it coming: the target
    // looked like a folder while the probe was out and turned out not to be
    // one. Everything else was refused by the cursor before the release.
    if (verdict.reason === "not-a-folder") {
      pushToast({
        msg: `"${basename(target.path)}" isn't a folder — nothing was moved.`,
        tone: "error",
      });
    }
    return;
  }
  const paths = dragged.map((d) => d.path);
  // The listing that OWNS the target performs the move — which after a
  // spring-loaded navigation is a different listing from the one the drag
  // started in, and it is the one that has to refresh and re-anchor.
  const targetScroller = spot.el.closest<HTMLElement>(".listing-scroll");
  const mover = (targetScroller ? movers.get(targetScroller) : undefined) ?? originMover;
  if (mover) mover(paths, verdict.dir, { announce: spot.announce });
  // No listing at all (both unmounted mid-drag): the move still happens, and
  // announces itself, because a move with nothing on screen to show it must.
  else void moveEntriesInto(paths, verdict.dir, { announce: true });
}

// One end for every way a drag can finish: dropped, released on nothing,
// cancelled, Escaped, or never begun at all. Idempotent, and it never performs
// the move — the caller does, from what this hands back.
function endDrag(): void {
  const d = live;
  if (!d) return;
  if (d.raf) cancelAnimationFrame(d.raf);
  clearSpot();
  d.ghost?.remove();
  if (d.active) {
    try {
      if (document.documentElement.hasPointerCapture(d.pointerId)) {
        document.documentElement.releasePointerCapture(d.pointerId);
      }
    } catch {
      /* the pointer is already gone; nothing to release */
    }
  }
  live = null;
  document.body.classList.remove(DRAGGING_CLASS, REFUSED_CLASS);
  window.removeEventListener("pointermove", onPointerMove);
  window.removeEventListener("pointerup", onPointerUp);
  window.removeEventListener("pointercancel", onPointerCancel);
  window.removeEventListener("keydown", onKeyDown, true);
  for (const h of springs) h.end();
  // The payload goes with the gesture. A drop has already taken its copy (see
  // onPointerUp), so this is the one place the in-flight store is emptied and
  // there is no path that leaves it holding a drag nobody is making.
  clearFsDrag();
}
