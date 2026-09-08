// THE ARITHMETIC, on its own and with no state (inventory §G, T:6667-6760,
// 7949, 8631-8673). Everything here is a function of its arguments, which is
// what makes the formulas testable without a browser — and the formulas are the
// part a rewrite gets subtly wrong.
//
// ONE coordinate space runs through all of it: the FRAMED VIEWPORT. Split, the
// iframe fills `#leftview`, so an element's rect inside the frame already is
// the stage rect; hosted, the layer host is `position: fixed; inset: 0` in the
// target's own document, so its box IS that viewport; XO, the overlay host is
// laid over the frame's rect in the parent. No conversion anywhere (T:6714).

/** T:6720 `annStageRect`'s answer shape. */
export interface AnnRect {
  left: number;
  top: number;
  width: number;
  height: number;
}

/** The two numbers a pin/popover clamp needs off its host, and nothing more —
 *  so a test can hand it a plain object. */
export interface StageBox {
  clientWidth: number;
  clientHeight: number;
}

/** T:6927 — a pin is `translate(-50%, -50%)` 22px round, so its centre stays a
 *  half-pin inside the stage on every edge. */
export const ANN_PIN_CLAMP = 14;
/** T:7341 — the composer card is 280px wide with 12px of clamp allowance. */
export const ANN_POP_W = 292;
/** T:7342 — and roughly this tall, empty. */
export const ANN_POP_H = 110;
/** T:6919 — the popover opens ten pixels down and right of the click. */
export const ANN_POP_OFFSET = 10;
/** T:6417 — the bar's height, which is also the margin a hosted document is
 *  pushed down by while it shows (T:6811). */
export const ANN_BAR_H = 43;

// ── pixel surfaces ──────────────────────────────────────────────────────────

/** T:6694 `annIntrinsic` — the natural size of a pixel surface, or null for
 *  anything else. A zero (an image that has not decoded) is "not yet", which is
 *  the same answer as "not a surface": there is no content box to be a fraction
 *  of. */
export function intrinsicOf(el: Element | null): { w: number; h: number } | null {
  if (!el || !el.tagName) return null;
  if (el.tagName === "IMG") {
    const img = el as HTMLImageElement;
    return img.naturalWidth > 0 ? { w: img.naturalWidth, h: img.naturalHeight } : null;
  }
  if (el.tagName === "VIDEO") {
    const vid = el as HTMLVideoElement;
    return vid.videoWidth > 0 ? { w: vid.videoWidth, h: vid.videoHeight } : null;
  }
  if (el.tagName === "CANVAS") {
    const cvs = el as HTMLCanvasElement;
    return cvs.width > 0 ? { w: cvs.width, h: cvs.height } : null;
  }
  return null;
}

/**
 * T:6701 `annContentBox` — where the pixels actually are inside the element's
 * box, which is not the box itself the moment `object-fit` letterboxes them.
 *
 * `fill` (and every value that is not one of the three scaling ones) paints the
 * whole box, so the box IS the content box and the rect comes back untouched.
 */
export function contentBox(el: Element, fitOf?: (el: Element) => string): AnnRect {
  const r = rectOf(el);
  const nat = intrinsicOf(el);
  if (!nat || !r.width || !r.height) return r;
  const fit = fitOf ? fitOf(el) : computedObjectFit(el);
  if (fit !== "contain" && fit !== "cover" && fit !== "scale-down") return r;
  return contentBoxOf(r, nat, fit);
}

/** The formula on its own — no DOM, so §G's numbers can be asserted directly. */
export function contentBoxOf(
  r: AnnRect,
  nat: { w: number; h: number },
  fit: "contain" | "cover" | "scale-down",
): AnnRect {
  let s =
    fit === "cover"
      ? Math.max(r.width / nat.w, r.height / nat.h)
      : Math.min(r.width / nat.w, r.height / nat.h);
  // `scale-down` is `contain` that never ENLARGES (T:6708).
  if (fit === "scale-down") s = Math.min(s, 1);
  const w = nat.w * s;
  const h = nat.h * s;
  return { left: r.left + (r.width - w) / 2, top: r.top + (r.height - h) / 2, width: w, height: h };
}

function computedObjectFit(el: Element): string {
  const win = el.ownerDocument && el.ownerDocument.defaultView;
  if (!win) return "fill";
  try {
    return win.getComputedStyle(el).objectFit || "fill";
  } catch {
    return "fill"; // a detached document mid-teardown
  }
}

/** T:6720 `annStageRect`. A plain copy rather than the live `DOMRect`, because
 *  every consumer stores it and a `DOMRect` is a view onto a layout that has
 *  since moved. */
export function rectOf(el: Element): AnnRect {
  const r = el.getBoundingClientRect();
  return { left: r.left, top: r.top, width: r.width, height: r.height };
}

// ── page ↔ viewport ─────────────────────────────────────────────────────────

/** The scroll a converter needs, and all of it: `ANN_XO_SCROLL` satisfies this
 *  shape, which is what lets the XO overlay share one converter (T:6141). */
export interface ScrollSource {
  scrollX?: number;
  scrollY?: number;
}

/** T:8631 — a point note is stored in PAGE coordinates, rounded, so it still
 *  means something after the app scrolls. */
export function pageXY(
  clientX: number,
  clientY: number,
  win: ScrollSource | null,
): { x: number; y: number } {
  return {
    x: Math.round(clientX + (win ? win.scrollX || 0 : 0)),
    y: Math.round(clientY + (win ? win.scrollY || 0 : 0)),
  };
}

/**
 * T:7949 `annPointXY` — and back again, through the CURRENT scroll rather than
 * the scroll at click time, exactly the way an element note's own
 * `getBoundingClientRect()` is always current.
 *
 * `null` off-window so callers fall back the same way a point with nothing to
 * draw already does.
 */
export function pointXY(
  c: { x?: number; y?: number },
  win: ScrollSource | null,
): { x: number; y: number } | null {
  if (!win || c.x == null || c.y == null) return null;
  return { x: c.x - (win.scrollX || 0), y: c.y - (win.scrollY || 0) };
}

/** T:8672 — the click's spot inside a painted content box, as fractions to
 *  three decimals, clamped to the box. */
export function iuivAt(
  clientX: number,
  clientY: number,
  b: AnnRect,
): { iu: number; iv: number } | null {
  if (!(b.width > 0) || !(b.height > 0)) return null;
  const f = (v: number) => Math.round(Math.min(1, Math.max(0, v)) * 1000) / 1000;
  return { iu: f((clientX - b.left) / b.width), iv: f((clientY - b.top) / b.height) };
}

// ── placement ───────────────────────────────────────────────────────────────

/**
 * T:6927 — where a pin goes, or `null` for "not this frame". Scrolled out of
 * the framed viewport draws NO pin (the chip stays); inside it, the centre is
 * held a half-pin off the left, right and top edges so a pin at the very edge
 * is still a whole pin.
 *
 * The bottom is deliberately unclamped, exactly as T leaves it: `y > height`
 * has already returned, so there is nothing left to push up.
 */
export function pinAt(x: number, y: number, host: StageBox): { left: number; top: number } | null {
  if (y < 0 || y > host.clientHeight || x < 0) return null;
  return {
    left: Math.min(host.clientWidth - ANN_PIN_CLAMP, Math.max(ANN_PIN_CLAMP, x)),
    top: Math.max(ANN_PIN_CLAMP, y),
  };
}

/** T:7341 — the composer, ten pixels down and right of the click and clamped so
 *  a click near an edge does not push the card off the pane. */
export function popAt(x: number, y: number, host: StageBox): { left: number; top: number } {
  return {
    left: Math.min(host.clientWidth - ANN_POP_W, Math.max(0, x + ANN_POP_OFFSET)),
    top: Math.min(host.clientHeight - ANN_POP_H, Math.max(0, y + ANN_POP_OFFSET)),
  };
}

/** T:6990 — where the composer opens when the CHIP was clicked rather than the
 *  pin. A point has its own coordinate even with nothing to resolve; an element
 *  note falls back to its rect's top-right; and an unresolvable one falls to the
 *  pane's thirds — except hosted, where a coordinate invented in someone else's
 *  viewport would put the card beside nothing, so `null` says "park it in this
 *  column instead". */
export function chipEditXY(
  point: { x: number; y: number } | null,
  rect: AnnRect | null,
  host: StageBox | null,
  hosted: boolean,
): { x: number | null; y: number | null } {
  if (point) return { x: point.x, y: point.y };
  if (rect) return { x: rect.left + rect.width, y: rect.top };
  if (hosted) return { x: null, y: null };
  return { x: (host ? host.clientWidth : 0) / 3, y: (host ? host.clientHeight : 0) / 3 };
}

/** T:6923 — the spot a note MARKS, given its resolved element. `iu`/`iv` name a
 *  pixel inside the painted content box; a plain element note is pinned at its
 *  top-right corner, which is where a badge would cover the least of it. */
export function elementPinXY(
  c: { iu?: number | null; iv?: number | null },
  box: AnnRect,
  rect: AnnRect,
  /** T:6919 asks `annIntrinsic(el)` alongside the fractions: a note hydrated
   *  from an older param may carry `iu`/`iv` for an element that is no longer a
   *  picture (or never was), and fractions of a plain box are a coordinate that
   *  moves with every reflow. Default true so the pure formula stays callable. */
  surface = true,
): { x: number; y: number } {
  if (surface && c.iu != null && c.iv != null) {
    return { x: box.left + c.iu * box.width, y: box.top + c.iv * box.height };
  }
  return { x: rect.left + rect.width, y: rect.top };
}

/** T:10163 — the OVERVIEW badges an element note at its CENTRE, not its corner:
 *  a badge is 22px of ink on the picture and a corner one straddles two
 *  elements. `iu`/`iv` still win, for the same reason they do for the pin. */
export function badgeXY(
  c: { iu?: number | null; iv?: number | null },
  box: AnnRect | null,
  rect: AnnRect,
  surface = true,
): { x: number; y: number } {
  if (surface && c.iu != null && c.iv != null && box) {
    return { x: box.left + c.iu * box.width, y: box.top + c.iv * box.height };
  }
  return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
}

// ── the element's name ──────────────────────────────────────────────────────

/**
 * T:6667 `annPathOf` — a `tag:nth-of-type(n)` chain from `<body>`, resolvable
 * with a plain `querySelector` against the app's source HTML. The SAME
 * identifier `pane/appState.ts`'s outline carries (its `PathOf` seam), which is
 * what lets a pin and an outline node name one element (D146).
 *
 * `null` when the walk does not reach `<body>` — a shadow tree, a detached
 * node, an element in another document.
 */
export function pathOf(el: Element, doc: Document): string | null {
  const segs: string[] = [];
  let node: Element | null = el;
  while (node && node !== doc.body && node.nodeType === 1) {
    let n = 1;
    let sib = node.previousElementSibling;
    while (sib) {
      if (sib.tagName === node.tagName) n++;
      sib = sib.previousElementSibling;
    }
    segs.unshift(node.tagName.toLowerCase() + ":nth-of-type(" + n + ")");
    node = node.parentElement;
  }
  return node === doc.body ? segs.join(">") : null;
}

const PATH_SEG = /^([a-z0-9-]+):nth-of-type\((\d+)\)$/i;

/**
 * T:6680 `annResolve` — the note's element again, or null. The id first,
 * because it survives a re-render the path does not.
 *
 * Case-INSENSITIVE on the tag, deliberately: HTML `tagName`s are uppercase and
 * SVG's stay lowercase, so a case-sensitive compare silently loses every note
 * anchored inside an `<svg>`.
 *
 * A path that resolves to `<body>` itself is NOT an answer (an empty path
 * string): the body is the whole page, and a note about the whole page is a
 * note about nothing in particular.
 */
export function resolveIn(
  c: { anchorId?: string; anchorPath?: string },
  doc: Document | null,
): Element | null {
  if (!doc || !doc.body) return null;
  if (c.anchorId) {
    const el = doc.getElementById(c.anchorId);
    if (el) return el;
  }
  if (!c.anchorPath) return null;
  let node: Element = doc.body;
  for (const seg of c.anchorPath.split(">")) {
    const m = PATH_SEG.exec(seg);
    if (!m) return null;
    let count = 0;
    let found: Element | null = null;
    for (const child of Array.from(node.children)) {
      if (child.tagName.toLowerCase() === m[1].toLowerCase() && ++count === parseInt(m[2], 10)) {
        found = child;
        break;
      }
    }
    if (!found) return null;
    node = found;
  }
  return node === doc.body ? null : node;
}

/** T:6733 `annLabelFor` — A, B, … Z, AA. Bijective base-26, so there is no
 *  letter that two notes can share. */
export function labelFor(i: number): string {
  let s = "";
  let n = i + 1;
  while (n > 0) {
    s = String.fromCharCode(65 + ((n - 1) % 26)) + s;
    n = Math.floor((n - 1) / 26);
  }
  return s;
}

// ── the bar's and the strip's folds: collision detection, never a breakpoint ─

/** The widths `barFolds` sums (T:6742). */
export interface BarMetrics {
  tag: number;
  slot: number;
  discard: number;
  done: number;
  stop: number;
}

/** T:6745 — tag + picker + trash + Done/■, with the row's gaps between
 *  whichever are PRESENT. A zero-width child holds no seat and pays no gap. */
export function barNeed(m: BarMetrics, gap: number): number {
  const parts = [m.slot, m.discard, m.done, m.stop].filter(Boolean);
  return m.tag + parts.reduce((a, b) => a + b, 0) + gap * parts.length;
}

/**
 * T:6733 `annBarFit` — everything on, then the sentence yields first (`t1`),
 * the buttons' words second (`t2`), the tag's word last (`t3`).
 *
 * `folded` is the SAME metrics re-measured with `t2` applied (the icon-only
 * buttons); the caller does that read, because it is a layout flush and this
 * function is arithmetic. Passing the same object twice is the honest answer
 * for a bar whose buttons carry no words to lose.
 *
 * The sentence is compared at its NATURAL width (`scrollWidth`): `offsetWidth`
 * has already been clipped by the ellipsis, so a folded bar could never tell
 * that there was room to unfold again.
 */
export function barFolds(
  box: number,
  gap: number,
  txtScrollWidth: number,
  wide: BarMetrics,
  folded: BarMetrics = wide,
): { t1: boolean; t2: boolean; t3: boolean } {
  const words = barNeed(wide, gap);
  const t1 = words + 10 + txtScrollWidth > box;
  const t2 = words > box;
  const t3 = t2 && barNeed(folded, gap) > box;
  return { t1, t2, t3 };
}

// T:7531 `annFitStrip` — the strip's own fold lives in `ui/useFitStrip`, which
// is the hook that measures the row it belongs to (A47). A second, pure copy of
// the same arithmetic was exported from here with no caller but its own test;
// one measurer per measurement.

/** T:7815 `annRecClock` / T:10273 `annClock` — m:ss for the live label. The
 *  wire's own copy lives in `protocol/wire.ts` (`annClock`) and stays there: a
 *  wire writer does not reach into the UI for a helper (T:10267). */
export function clockOf(seconds: number): string {
  return Math.floor(seconds / 60) + ":" + String(Math.floor(seconds % 60)).padStart(2, "0");
}

/** T:7943 `annRecStamp` — seconds into a recording, to a TENTH. Rounded because
 *  nothing downstream wants the rest: the raw division put seventeen digits per
 *  note on the wire (2.4150000000372529) for a reader that shows two. */
export function stampOf(elapsedMs: number): number {
  return Math.round(elapsedMs / 100) / 10;
}
