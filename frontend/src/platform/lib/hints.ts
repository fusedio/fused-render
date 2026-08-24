// The app's one INSTANT tooltip, positioned at the POINTER.
//
// It exists because the two obvious answers are each wrong in one way, and this
// page needed both halves. A native `title` is placed perfectly — the browser
// draws it at the cursor, so it can never caption the wrong row and can never be
// clipped by a scroller — but its delay is the browser's and unreachable from
// CSS: measured on the Tasks list, the FIRST hover of a session waits four to
// five seconds ("the very first time I hover, area label takes 4 to 5 seconds").
// A CSS `::before` panel is instant and unplaceable: it is absolutely positioned
// against its own element, so on the first row of a list it opens over the row
// below, and on the last cell of a row it hangs outside the scroller. Both of
// those shipped here and both were reported.
//
// Following the pointer is what has neither problem. One fixed-position element
// for the whole document, moved to the cursor, shown with no delay:
//
//   * INSTANT, because nothing schedules it — `pointerover` shows it.
//   * never displaced, because the cursor is by definition where the reader is
//     looking, so the caption cannot land on a different row's ink.
//   * never clipped, because `position: fixed` on a child of <body> is outside
//     every `overflow` on the page.
//
// Opt in with `data-hint="…"`. Deliberately NOT `data-tip`, which is the older
// CSS panel and still the right tool for the one thing it does — the status
// ring's unread count, a mark in a COLUMN of identical marks, where a 300ms
// guard against strobing is the point rather than an obstacle.
//
// One delegated listener set for the whole document rather than a React
// component per tip: the tips are on rows in a list that can hold hundreds, and
// a component and a piece of state per mark is a lot of machinery to say a file
// path.

/** How far the panel sits from the cursor's hotspot. Below and right, like the
 *  platform's own tooltips, so the panel never covers the thing being pointed
 *  at — a caption you have to move the pointer off to read is worse than none. */
const OFFSET_X = 12;
const OFFSET_Y = 18;
/** Keep the panel this far inside the viewport before flipping it. */
const EDGE = 8;

let panel: HTMLDivElement | null = null;
let host: Element | null = null;
let installed = false;

function ensurePanel(): HTMLDivElement {
  if (panel) return panel;
  panel = document.createElement("div");
  panel.className = "hint-panel";
  // `aria-hidden`, and it is not an oversight: every element that opts in also
  // carries its own accessible name (see the call sites), so announcing this
  // panel too would say the same sentence twice to a screen reader. This is a
  // drawing of something the DOM already states.
  panel.setAttribute("aria-hidden", "true");
  document.body.appendChild(panel);
  return panel;
}

/** The nearest ancestor with a hint, or null — resolved from an ELEMENT. Used by
 *  the focus path, where there is no pointer to ask about.
 *
 *  An EMPTY hint stops the walk and answers null rather than deferring to an
 *  ancestor. That is the opt-out: a band around a control (see the Tasks list's
 *  `.schedule-tv-id-shield`) has to be able to say "nothing here" inside a
 *  region whose parent does have a caption. */
function hintOf(target: EventTarget | null): Element | null {
  if (!(target instanceof Element)) return null;
  const el = target.closest("[data-hint]");
  if (!el) return null;
  return (el.getAttribute("data-hint") || "").trim() ? el : null;
}

/** The hinted element under a POINT, piercing overlays.
 *
 *  `event.target` alone is not enough, and the case that proves it is the Tasks
 *  row: its navigation is an `<a>` stretched over the whole row, so the pointer
 *  lands on the LINK and never on the title underneath it — and the title is
 *  what carries the caption. Lifting the title above the link instead would take
 *  the click with it and stop the row from opening, so the fix belongs here.
 *
 *  `elementsFromPoint` gives the whole stack, topmost first. The first entry
 *  that resolves to a hinted element decides — including deciding NOTHING, when
 *  that element's hint is empty, so an opt-out placed above a caption still
 *  wins. */
function hintAt(x: number, y: number, target: EventTarget | null): Element | null {
  if (typeof document.elementsFromPoint !== "function") return hintOf(target);
  for (const node of document.elementsFromPoint(x, y)) {
    const el = node.closest("[data-hint]");
    if (!el) continue;
    return (el.getAttribute("data-hint") || "").trim() ? el : null;
  }
  return null;
}

function place(x: number, y: number): void {
  const p = ensurePanel();
  // Measured after the text is in, because the flip depends on the width.
  const w = p.offsetWidth;
  const h = p.offsetHeight;
  let left = x + OFFSET_X;
  let top = y + OFFSET_Y;
  // Flip rather than clamp when the panel would leave the viewport: a clamped
  // panel sits under the cursor and covers what the reader is pointing at.
  if (left + w > window.innerWidth - EDGE) left = Math.max(EDGE, x - OFFSET_X - w);
  if (top + h > window.innerHeight - EDGE) top = Math.max(EDGE, y - OFFSET_Y - h);
  p.style.left = `${Math.round(left)}px`;
  p.style.top = `${Math.round(top)}px`;
}

function show(text: string, x: number, y: number): void {
  const p = ensurePanel();
  p.textContent = text;
  p.classList.add("is-on");
  place(x, y);
}

export function hideHint(): void {
  host = null;
  if (panel) {
    panel.classList.remove("is-on");
    // Emptied as well as hidden: a stale string in a hidden panel is a string
    // that flashes on the next show, before its own text lands.
    panel.textContent = "";
  }
}

function onOver(e: PointerEvent): void {
  const el = hintAt(e.clientX, e.clientY, e.target);
  if (!el) {
    if (host) hideHint();
    return;
  }
  host = el;
  show(el.getAttribute("data-hint") || "", e.clientX, e.clientY);
}

function onMove(e: PointerEvent): void {
  // Asked on EVERY move rather than only while a hint is up, because the
  // element under the pointer can change without any `pointerover` this sees:
  // the row's stretched link is one continuous element, so moving from the
  // title onto the empty space beside it never crosses an event boundary even
  // though the answer changes from "the task's name" to "nothing".
  const el = hintAt(e.clientX, e.clientY, e.target);
  if (!el) {
    if (host) hideHint();
    return;
  }
  if (el !== host) {
    host = el;
    show(el.getAttribute("data-hint") || "", e.clientX, e.clientY);
    return;
  }
  place(e.clientX, e.clientY);
}

function onOut(e: PointerEvent): void {
  if (!host) return;
  // `relatedTarget` is where the pointer went. Moving between two children of
  // the same hinted element must not flicker the panel off and on.
  if (hintOf(e.relatedTarget) === host) return;
  hideHint();
}

/** Keyboard focus gets the same caption, anchored to the ELEMENT rather than to
 *  a pointer that is not there. Without this the hint is a mouse-only feature,
 *  which for a control whose only explanation is its hint is a control a
 *  keyboard cannot understand. */
function onFocus(e: FocusEvent): void {
  const el = hintOf(e.target);
  if (!el) return;
  const r = el.getBoundingClientRect();
  host = el;
  show(el.getAttribute("data-hint") || "", r.left + r.width / 2 - OFFSET_X, r.bottom - OFFSET_Y + 6);
}

/** Install the one listener set. Idempotent, so a re-render or a second caller
 *  cannot end up with two panels or a doubled listener set. */
export function installHints(): void {
  if (installed || typeof document === "undefined") return;
  installed = true;
  // `capture: true` on all of them: a row that calls `stopPropagation` on its
  // own pointer events (this app has several) would otherwise silence the hint
  // for everything inside it.
  document.addEventListener("pointerover", onOver, true);
  document.addEventListener("pointermove", onMove, true);
  document.addEventListener("pointerout", onOut, true);
  document.addEventListener("focusin", onFocus, true);
  document.addEventListener("focusout", hideHint, true);
  // A press means the reader has decided; the caption has nothing left to add,
  // and on a control that navigates it would otherwise outlive the page.
  document.addEventListener("pointerdown", hideHint, true);
  // Anything that moves the page out from under a fixed panel: the panel is
  // anchored to viewport coordinates the scroll has just invalidated.
  window.addEventListener("scroll", hideHint, true);
  window.addEventListener("blur", hideHint);
}
