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
 *  wins.
 *
 *  PIERCING STOPS AT A MODAL. `elementsFromPoint` walks the whole stack
 *  regardless of what visually blocks the pointer — a stretched link over a
 *  row's own title and a New Task dialog sitting over that same row read
 *  identically to this loop, "an element with no hint, keep going." That
 *  correctly reaches the title in the first case (same row, sharing this
 *  pointer's screen position on purpose) and wrongly reaches it in the second
 *  (a DIFFERENT row's title, portalled to <body>, that has nothing to do with
 *  the dialog above it) — hovering anywhere over the New Task card showed the
 *  caption of whatever List row happened to sit under it (Akshil, 2026-09-03).
 *  A dialog's overlay (`.modal-overlay`, every modal's chassis — Modal.tsx,
 *  FsDialogs) covers the full viewport and owns every point inside it, so once
 *  the walk reaches it the answer is decided: nothing further down the stack —
 *  behind the backdrop — ever gets to caption this pointer.
 *
 *  A POPOVER THAT IS NOT A MODAL opts in with `data-hint-stop`: the Tasks
 *  tab editor's + menu hangs over the list, and hovering "PR" in it captioned
 *  the task row underneath (Akshil, 2026-10-16). Same rule, smaller surface. */
function hintAt(x: number, y: number, target: EventTarget | null): Element | null {
  if (typeof document.elementsFromPoint !== "function") return hintOf(target);
  for (const node of document.elementsFromPoint(x, y)) {
    const el = node.closest("[data-hint]");
    if (el) return (el.getAttribute("data-hint") || "").trim() ? el : null;
    if (node.closest(".modal-overlay, [data-hint-stop]")) return null;
  }
  return null;
}

/** THE TASK FORM FOLLOWS THE POINTER INSIDE THE MIDDLE 80% OF THE VIEWPORT
 *  (Akshil, 2026-09-19): its LEFT edge starts at the pointer, like every other
 *  caption, and it is CLAMPED to the band rather than flipped — the old flip
 *  swung a 90ch panel its whole width on one pixel of travel near the right
 *  edge. At the band's end the panel stops and the pointer walks on without it. */
const BAND = 0.8;
function placeTask(x: number, y: number): void {
  const p = ensurePanel();
  const w = shownW;
  const h = shownH;
  const vw = window.innerWidth;
  const bandLeft = vw * (1 - BAND) / 2;
  const bandRight = vw - bandLeft;
  let left = x + OFFSET_X;
  if (w >= bandRight - bandLeft) left = (vw - w) / 2;
  else left = Math.min(bandRight - w, Math.max(bandLeft, left));
  let top = y + OFFSET_Y;
  if (top + h > window.innerHeight - EDGE) top = Math.max(EDGE, y - OFFSET_Y - h);
  p.style.left = `${Math.round(left)}px`;
  p.style.top = `${Math.round(top)}px`;
}

/** The panel's width with the whole viewport to lay out in. A `position:
 *  fixed` box with a `left` set shrink-wraps to the room to its RIGHT, so a
 *  panel measured where the last hint left it — near the right edge — reads
 *  narrower than it will be once moved, and the clamp computed from that
 *  width lets it overflow. That is the "entered from the right without
 *  moving" bug: one placement, from a stale left. Measured at 0 instead. */
function measureWidth(p: HTMLDivElement): number {
  p.style.left = "0px";
  return p.offsetWidth;
}

/** THE PANEL'S SIZE, READ ONCE PER SHOW (2026-10-09). `place` runs on every
 *  pointer move while a hint is up, and `measureWidth` is a style write
 *  followed by a layout read — a forced layout per move, which on the Tasks
 *  list is a style pass over every mounted row per move. The text does not
 *  change between a show and the next; its size does not either. */
let shownW = 0;
let shownH = 0;
function sizeOnce(p: HTMLDivElement): void {
  shownW = measureWidth(p);
  shownH = p.offsetHeight;
}

/** ANCHORED BESIDE ITS ELEMENT, NOT AT THE POINTER: `data-hint-side="left"`.
 *  For a hint on a control that sits at the window's edge in a column of its
 *  own — the Claude rail (shell/claude-rail/ClaudeRail), a ring per
 *  chat 48px from the right edge. Pointer placement put the panel below the
 *  cursor and, for the task form, clamped it into the middle band, so it hung
 *  hundreds of pixels left of the ring it described. Here it stands just left
 *  of the element, centred on it vertically and kept inside the viewport, and
 *  it does not follow the pointer within the element. */
function anchoredSide(el: Element | null): "left" | null {
  return el?.getAttribute("data-hint-side") === "left" ? "left" : null;
}
function placeBeside(el: Element): void {
  const p = ensurePanel();
  const r = el.getBoundingClientRect();
  const w = shownW;
  const h = shownH;
  // The row form stands where the column's own windows stand: against the
  // edge its host names (`data-hint-edge`), not against the one ring.
  const edge = p.classList.contains("is-row") ? rowEdge(el) : r.left;
  const left = Math.max(EDGE, edge - 8 - w);
  const top = Math.min(
    window.innerHeight - EDGE - h,
    Math.max(EDGE, r.top + r.height / 2 - h / 2),
  );
  p.style.left = `${Math.round(left)}px`;
  p.style.top = `${Math.round(top)}px`;
}

function place(x: number, y: number): void {
  const p = ensurePanel();
  if (host && anchoredSide(host)) {
    placeBeside(host);
    return;
  }
  // The band is for the ROW's two-line form, whose 90ch panel used to swing
  // on a flip. The Board card's reply-only form is a short caption and starts
  // at the cursor like every other hint (Akshil, 2026-09-20: "let's not apply
  // it for the kanban board, have the tooltip start from the cursor").
  if (p.classList.contains("is-task") && !p.classList.contains("is-reply")) {
    placeTask(x, y);
    return;
  }
  // Measured after the text is in (`sizeOnce`, at show), because the flip
  // depends on the width.
  const w = shownW;
  const h = shownH;
  let left = x + OFFSET_X;
  let top = y + OFFSET_Y;
  // Flip rather than clamp when the panel would leave the viewport: a clamped
  // panel sits under the cursor and covers what the reader is pointing at.
  if (left + w > window.innerWidth - EDGE) left = Math.max(EDGE, x - OFFSET_X - w);
  if (top + h > window.innerHeight - EDGE) top = Math.max(EDGE, y - OFFSET_Y - h);
  p.style.left = `${Math.round(left)}px`;
  p.style.top = `${Math.round(top)}px`;
}

/** The separator a caption uses to say "this token, then what it means". */
const GLOSS = " \u2014 ";

/** A caption whose every line is `token — meaning` is a TABLE, and setting it
 *  as text renders it as prose: the dashes land wherever each token happens to
 *  end, so the meanings never form a column and the tokens get no distinction
 *  from the sentences explaining them. Two spans a line and a grid gives the
 *  reader the shape the caption already has.
 *
 *  Every line has to qualify. A caption with one glossed line among prose is
 *  prose with a dash in it, and columnising it would strand the rest. */
/** A PLAIN CAPTION'S NUMBERS KEEP THE PANEL'S WEIGHT AND ITS WORDS GO LIGHTER
 *  (Akshil, 2026-10-06): `3m ago · ran 4s` is two facts and some grammar, and
 *  the facts are the digits. A digit run with its unit glued on (`4s`, `22:42`,
 *  `2026`) stays as is; everything else wears `.hint-word`. Text nodes and
 *  spans only — the caption is still never parsed as markup. */
/** A digit run with its unit (`4s`, `22:42`, `2026`) — and the month that
 *  follows a day (`6 Oct`), which is the date's own word, not grammar
 *  (Akshil, 2026-10-06: "Oct should still be heavy"). */
const NUMBER_RUN = /(\d[\d:.]*[a-z]?(?: (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b)?)/g;

function renderWords(p: HTMLDivElement, text: string): void {
  for (const part of text.split(NUMBER_RUN)) {
    if (!part) continue;
    if (/^\d/.test(part)) {
      p.appendChild(document.createTextNode(part));
      continue;
    }
    const w = document.createElement("span");
    w.className = "hint-word";
    w.textContent = part;
    p.appendChild(w);
  }
}

function renderHint(p: HTMLDivElement, text: string): void {
  const lines = text.split("\n");
  if (lines.length < 2 || !lines.every((line) => line.includes(GLOSS))) {
    p.textContent = "";
    renderWords(p, text);
    return;
  }
  const grid = document.createElement("div");
  grid.className = "hint-grid";
  for (const line of lines) {
    const cut = line.indexOf(GLOSS);
    const key = document.createElement("span");
    key.className = "hint-key";
    key.textContent = line.slice(0, cut);
    const def = document.createElement("span");
    def.className = "hint-def";
    def.textContent = line.slice(cut + GLOSS.length);
    grid.append(key, def);
  }
  p.replaceChildren(grid);
}

/** THE TASK ROW'S OWN FORM (Akshil, 2026-09-19): hovering the title or the
 *  reply shows BOTH, untruncated, in the styles the row prints them in — the
 *  title's line, then the reply's. Opted into with `data-hint-title` (and an
 *  optional `data-hint-reply`) beside `data-hint`; `data-hint` stays as the
 *  plain-text fallback so `hintAt` resolves the element the same way.
 *
 *  Either line may be left out — an empty `data-hint-title` with a reply is
 *  the Board card's REPLY-ONLY caption (Akshil, 2026-09-20: "let's not show
 *  the title, only the last reply"), still in the reply line's own style.
 *  Both empty is not this form at all, and the plain `data-hint` draws. */
function renderTaskHint(p: HTMLDivElement, el: Element): boolean {
  if (!el.hasAttribute("data-hint-title")) return false;
  const title = (el.getAttribute("data-hint-title") || "").trim();
  const reply = (el.getAttribute("data-hint-reply") || "").trim();
  if (!title && !reply) return false;
  // The reply-only form is marked, because it is PLACED differently (see
  // `place`) and reads narrower (tasks.css `.is-reply`).
  p.classList.toggle("is-reply", !title);
  const wrap = document.createElement("div");
  wrap.className = "hint-task";
  if (title) {
    const t = document.createElement("div");
    t.className = "hint-task-title";
    t.textContent = title;
    wrap.append(t);
  }
  if (reply) {
    const r = document.createElement("div");
    r.className = "hint-task-reply";
    r.textContent = reply;
    wrap.append(r);
  }
  p.replaceChildren(wrap);
  return true;
}

/** The x the row form is anchored against: the left edge of the nearest
 *  ancestor that says it is the column (`data-hint-edge`), else the host's. */
function rowEdge(el: Element): number {
  const col = el.closest("[data-hint-edge]") ?? el;
  return col.getBoundingClientRect().left;
}

/** The message glyph the count wears (lucide `message-square`, 12px). */
const ROW_COUNT_ICON =
  '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>';

/** THE TASK'S NAME AND ITS FACTS (Akshil, 2026-11-16: "when we hover we show
 *  a tooltip with the title of the task — instead show this as is") beside a
 *  rail ring whose hover opens no card. It was as wide as the window the ring
 *  opens until 2026-11-18 ("make it not the same width as the window, we can
 *  have it less or more"); it is as wide as its words now, see below. It began as the Tasks list's whole row —
 *  status, number, title, reply, count, time. Since 2026-11-17 the LEFT part
 *  is gone and the title is never clamped ("cut the left side, like icon and
 *  task id … get rid of truncation and keep only title, not subtitle as
 *  well"; then, of a build that had dropped the facts too: "I asked only to
 *  strip the left part, that is status and task id, keep the time and count
 *  as is. And don't truncate it as well"): the title, whole — it wraps when
 *  it is longer than the room — then the message count and the time at the
 *  right end, on the title's first line. No ring, no number, no reply.
 *  Opted into with `data-hint-row` beside the task form's `data-hint-title`;
 *  `data-hint-count` and `data-hint-when` are the facts,
 *  `data-hint-window` the window the ring opens
 *  ("a" | "b" — the one the scrim dims, tasks.css). */
function renderRowHint(p: HTMLDivElement, el: Element): boolean {
  if (!el.hasAttribute("data-hint-row")) return false;
  const title = (el.getAttribute("data-hint-title") || "").trim();
  if (!title) return false;
  const span = (cls: string, text: string): HTMLSpanElement => {
    const s = document.createElement("span");
    s.className = cls;
    s.textContent = text;
    return s;
  };
  const row = document.createElement("div");
  row.className = "hint-row";
  row.append(span("hint-row-title", title));
  const count = (el.getAttribute("data-hint-count") || "").trim();
  if (count) {
    const c = span("hint-row-count", count);
    c.insertAdjacentHTML("beforeend", ROW_COUNT_ICON);
    row.append(c);
  }
  const when = (el.getAttribute("data-hint-when") || "").trim();
  if (when) row.append(span("hint-row-time", when));
  p.replaceChildren(row);
  // WHICH WINDOW THIS RING OPENS: only that one steps back under the scrim
  // (Akshil, 2026-11-17: "it darkens both instead of only … one of the
  // window"). A host that does not say is window A's.
  p.setAttribute("data-row-window", el.getAttribute("data-hint-window") === "b" ? "b" : "a");
  // AS WIDE AS ITS WORDS, NOT AS THE WINDOW (Akshil, 2026-11-18: "the width,
  // make it not the same width as the window, we can have it less or more …
  // if it is the same width that's fine for one or two but not for all,
  // basically it is fine if it is coincidence"). The panel fits its content
  // (the fixed panel's own shrink-to-fit), up to the row's measure
  // (tasks.css) and never past the room beside the column — which is all
  // this says: a title longer than that wraps.
  const room = rowEdge(el) - 8 - EDGE;
  p.style.setProperty("--hint-row-room", `${Math.max(120, Math.round(room))}px`);
  return true;
}

/** THE LIST MOVED, THE POINTER DID NOT (Akshil, 2026-11-17: "when I scroll
 *  there is a little flash of the overlay that comes in and goes out"). A
 *  scroll hides the hint; the browser then reports the element that slid
 *  under the resting pointer, and its hint comes up — until the next scroll
 *  tick hides it again. For a caption that is harmless. A rail ring's ROW
 *  also dims the rail's windows (tasks.css, the scrim; `markRowDim`), so
 *  every tick of the wheel blinked the window. A row that came up this way is
 *  marked `is-passing` and dims nothing; the mark goes the moment the pointer
 *  is somewhere new — the reader pointing — or the keyboard brings a hint. */
let passing = false;

/** WHICH RAIL WINDOW STANDS UNDER THE SCRIM, SAID ON THE ROOT ELEMENT
 *  (tasks.css reads `:root[data-rail-row-dim]`): the row's window while a row
 *  is up and the reader is pointing at it, nothing otherwise.
 *
 *  ON THE ROOT, NOT ASKED OF THE PANEL WITH `body:has(.hint-panel…)` (Akshil,
 *  2026-11-18: "the render app feels a bit slow, especially when moving around
 *  and resizing the tasks"). A `:has()` whose anchor is `body`, with a
 *  descendant after it, makes the browser treat every element inserted or
 *  removed ANYWHERE as something that may have changed the answer — and the
 *  shadcn `group-has-*` rules make that invalidation the whole document. Every
 *  insert restyled all ~5,800 elements (12.5 ms each, against 0.15 ms), and a
 *  rail window's resize ran at 34 to 48 frames a second. An attribute written
 *  here costs a restyle when the row comes up and when it goes, and nothing in
 *  between. */
const ROW_DIM_ATTR = "data-rail-row-dim";
function markRowDim(): void {
  const root = document.documentElement;
  const dims = !!panel && panel.classList.contains("is-on") && panel.classList.contains("is-row") && !panel.classList.contains("is-passing");
  const win = dims ? panel!.getAttribute("data-row-window") : null;
  if (!win) {
    if (root.hasAttribute(ROW_DIM_ATTR)) root.removeAttribute(ROW_DIM_ATTR);
    return;
  }
  if (root.getAttribute(ROW_DIM_ATTR) !== win) root.setAttribute(ROW_DIM_ATTR, win);
}

let pointerX = Number.NaN;
let pointerY = Number.NaN;

function onScrolled(): void {
  passing = true;
}

/** A pointer event at a NEW spot is the reader pointing. The events a scroll
 *  causes under a pointer at rest carry the spot it already had.
 *
 *  THE FIRST EVENT IS A BASELINE, NOT A MOVE (CodeRabbit on #2320): until one
 *  has been seen there is no spot to compare with, and a pointer already
 *  resting on the rail when the page loaded would have its first scroll-made
 *  event read as "somewhere new" — clearing the mark and dimming the window
 *  with nothing having moved. */
function pointed(e: PointerEvent): void {
  const known = !Number.isNaN(pointerX) && !Number.isNaN(pointerY);
  if (known && e.clientX === pointerX && e.clientY === pointerY) return;
  pointerX = e.clientX;
  pointerY = e.clientY;
  if (!known) return;
  if (!passing) return;
  passing = false;
  if (panel) panel.classList.remove("is-passing");
  markRowDim();
}

function show(el: Element, x: number, y: number): void {
  const p = ensurePanel();
  const row = renderRowHint(p, el);
  p.classList.toggle("is-row", row);
  p.classList.toggle("is-passing", row && passing);
  if (!row) {
    p.style.removeProperty("--hint-row-room");
    p.removeAttribute("data-row-window");
  }
  const task = !row && renderTaskHint(p, el);
  if (!row && !task) renderHint(p, el.getAttribute("data-hint") || "");
  p.classList.toggle("is-task", task);
  if (!task) p.classList.remove("is-reply");
  p.classList.add("is-on");
  markRowDim();
  sizeOnce(p);
  if (anchoredSide(el)) {
    placeBeside(el);
    return;
  }
  place(x, y);
}

export function hideHint(): void {
  host = null;
  // Nothing to do for a panel that is already down — and this runs on every
  // scroll event of every scroller in the document.
  if (panel && panel.classList.contains("is-on")) {
    panel.classList.remove("is-on");
    // Emptied as well as hidden: stale content in a hidden panel is content
    // that flashes on the next show, before its own text lands.
    panel.replaceChildren();
    panel.classList.remove("is-row");
    panel.removeAttribute("data-row-window");
    panel.classList.remove("is-passing");
    panel.style.removeProperty("--hint-row-room");
    markRowDim();
  }
}

function onOver(e: PointerEvent): void {
  pointed(e);
  const el = hintAt(e.clientX, e.clientY, e.target);
  if (!el) {
    if (host) hideHint();
    return;
  }
  host = el;
  show(el, e.clientX, e.clientY);
}

/** Does the pointer need the POINT resolved on this move? Only over an
 *  element that opts in with `data-hint-through`: the Tasks row's stretched
 *  link, one continuous element whose hint is the title's underneath it, so
 *  moving from the title onto the empty space beside it never crosses an
 *  event boundary even though the answer changes from "the task's name" to
 *  "nothing". Everywhere else `pointerover` / `pointerout` already say when
 *  the element under the pointer changed, and `elementsFromPoint` on every
 *  move — a hit test over the whole document, 60+ times a second — was
 *  measured as a share of the Tasks list's frame cost (2026-10-09). */
function throughAt(target: EventTarget | null): boolean {
  return target instanceof Element && target.closest("[data-hint-through]") !== null;
}

function onMove(e: PointerEvent): void {
  pointed(e);
  if (!host && !throughAt(e.target)) return;
  if (host && !throughAt(e.target)) {
    // The hint's own element is under the pointer (pointerout would have
    // hidden it otherwise); just follow.
    place(e.clientX, e.clientY);
    return;
  }
  const el = hintAt(e.clientX, e.clientY, e.target);
  if (!el) {
    if (host) hideHint();
    return;
  }
  if (el !== host) {
    host = el;
    show(el, e.clientX, e.clientY);
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
  // The keyboard brought this one: it is the reader's, whatever scrolled.
  passing = false;
  show(el, r.left + r.width / 2 - OFFSET_X, r.bottom - OFFSET_Y + 6);
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
  // …and whatever comes up next under a pointer that has not moved is the
  // list passing, not the reader pointing (`passing`).
  window.addEventListener("scroll", onScrolled, true);
  window.addEventListener("blur", hideHint);
}
