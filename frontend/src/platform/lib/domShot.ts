// Rasterise a same-origin document by CLONING ITS DOM — the technique the
// Claude chat template's `shotPane` uses, ported here so the shell's export /
// preview capture can use it too (D635).
//
// The pipeline, in the order the steps must run:
//   1. `body.cloneNode(true)` — a detached copy of the app's tree.
//   2. `inlineComputedStyles` — `<style>` rules do NOT travel with a serialized
//      element, so every computed longhand is copied onto the clone's inline
//      style. This is what makes the picture look like the app instead of like
//      unstyled HTML, and it is by a wide margin the expensive step.
//   3. `inlineImages` — every `<img>` / `url(...)` rewritten to a `data:` URL,
//      because the SVG below is loaded as a data: URL and cannot reach out.
//   4. `rasteriseCanvases` — a `<canvas>` serializes as an EMPTY element, so
//      each one is swapped for an `<img>` of its own pixels.
//   5. `applyScrollOffsets` — LAST, so a canvas swapped for an `<img>` in step 4
//      is shifted with the rest of its scroll box rather than left at the top.
//   6. serialize to an SVG `<foreignObject>`, load it as an `<img>`, draw it on
//      a canvas.
//
// ── why the clone is styled from the LIVE tree ──
//
// A clone is detached, and a detached element has no cascade:
// `getComputedStyle` on one enumerates ZERO properties. Putting the clone into
// the app's document to give it a cascade would double the DOM, duplicate every
// id, change what sibling/nth-child selectors match, and mutate the page the
// user is looking at. So the live tree is the only source of truth for computed
// style — which is what creates the race `inlineComputedStyles` defends
// against, since that walk yields.
//
// ── the known cost ──
//
// A WebGL pane rasterises BLANK. A map/3D library (maplibre, deck.gl) creates
// its context with `preserveDrawingBuffer: false`, so `toDataURL` reads back
// transparent or throws, and step 4 leaves the hole a `<canvas>` would have
// been anyway. `blanks` counts them so a caller can say so. This is inherent to
// DOM serialization and is not worked around here.

// Elements styled before the walk gives up. A huge page costs a PARTLY styled
// capture instead of an unbounded one — which also bounds the serialized SVG,
// since that grows by a few KB of inline style per element.
const MAX_ELEMENTS = 3000;

// Elements styled between yields. The walk yields BECAUSE a synchronous
// recursion makes a deadline a promise it cannot keep: a timer cannot fire
// while synchronous code runs, so the whole UI froze for however long the walk
// took. Yielding is what makes the deadline able to fire at all.
const STYLE_CHUNK = 200;

// How much of the budget is reserved for the image fetches. The style walk
// stops SHORT of the overall deadline by this much so the fetches have a tail
// to run in — sharing one stamp would let a big page spend the entire budget
// and leave every picture inside it a placeholder in a capture that had time
// in hand.
const IMG_TAIL_MS = 1500;

// Distinct URLs fetched per capture, and the size above which one is re-encoded
// smaller before being inlined. base64 adds a third on top of the bytes and the
// result is inlined in markup that then has to be parsed, rasterised and
// encoded again — so a 4 MB photo would cost more than the whole picture it
// appears in.
const IMG_MAX = 30;
const IMG_MAX_BYTES = 256 * 1024;

// The edge an oversized inlined image is re-encoded down to.
const IMG_MAX_EDGE = 1600;

// Total wall-clock a capture gets by default.
export const SHOT_TIMEOUT_MS = 8000;

// One scrolled box, paired with its clone. Applied by `applyScrollOffsets`
// AFTER the style walk, because the children that get shifted are still being
// styled while it runs.
export interface ScrolledBox {
  clone: Element;
  x: number;
  y: number;
}

// One cause, never a set of booleans that can disagree. Precedence when several
// apply: a re-render may have made styles WRONG anywhere in the capture, while
// a cap only leaves them MISSING past a point — correctness news outranks
// budget news.
export type Incomplete = "" | "mutated" | "detached" | "elements" | "deadline";

export interface InlineStylesResult {
  styled: number;
  incomplete: Incomplete;
  scrolled: ScrolledBox[];
}

// Minimal shape of the things this module needs off a Window, so a caller can
// hand in an iframe's `contentWindow` (typed `Window | null`) without a cast
// and a test can hand in a stub.
type StyleView = Pick<Window, "getComputedStyle">;

function viewOf(el: Element): (StyleView & { MutationObserver?: typeof MutationObserver }) | null {
  if (!el.isConnected) return null;
  const doc = el.ownerDocument;
  const view = doc ? doc.defaultView : null;
  return view as (StyleView & { MutationObserver?: typeof MutationObserver }) | null;
}

// Copy every computed property off the live tree onto the clone, breadth-first.
//
// Breadth-first rather than depth-first BECAUSE it can stop early: the elements
// nearest `<body>` are the layout containers, so a truncated capture still has
// the page's overall shape rather than one fully-styled subtree and nothing
// else.
//
// ── the price of yielding: the source is LIVE ──
//
// The synchronous walk was slow but ATOMIC. Yielding bought boundedness and
// gave up atomicity: `src` is the app's own tree, `dst` a clone taken before
// the walk began, and between chunks the app can re-render — an interval that
// re-renders a list is enough. Three things then go wrong:
//   a queued source node can leave the document, where `getComputedStyle`
//     returns nothing usable, so reading it writes an authoritative "no
//     styling";
//   `defaultView` can be null once the frame navigates, which is a TypeError
//     out of the middle of a capture rather than a degradation;
//   and `s.children` / `d.children` can stop corresponding, so `a[i]`/`b[i]`
//     pairs a live node with the WRONG clone node and every style past that
//     point lands on the wrong element.
//
// Atomicity is not recoverable (see the module header for why the clone cannot
// be styled from its own document), so the property kept instead is: NEVER
// write a style we are not sure belongs to that node, and always report when we
// could not. A missing style degrades a picture; a wrong one lies about it.
//
// Note WHERE the mispairing comes from, because it decides the fix: a queued
// pair holds direct node references, so a pair formed BEFORE a re-render stays
// correct however the live tree is shuffled afterwards. Only pairs formed AFTER
// it are wrong. So the rule is not "abandon the capture", it is "never descend
// into a parent whose children have moved since the clone was taken".
//
// Detected two ways, both needed: a MutationObserver names every element whose
// child list changed — which covers the case no structural check can see, a
// list rebuilt or reordered with the SAME number of children — and a
// child-count comparison is the floor for anything the observer misses.
//
// Reported, not retried: a retry from a fresh clone doubles the cost inside a
// budget that is already tight, and an app re-rendering on an interval will
// just mutate again.
export async function inlineComputedStyles(
  src: Element,
  dst: Element,
  deadline: number,
  maxElements: number = MAX_ELEMENTS,
): Promise<InlineStylesResult> {
  let styled = 0;
  let stopped: Incomplete = "";
  let detached = false;
  let mutated = false;
  const scrolled: ScrolledBox[] = [];
  // Elements whose child list has changed since the clone was taken, so
  // descending into them would pair the wrong nodes.
  const reshaped = new Set<Node>();

  const absorb = (records: MutationRecord[] | undefined): void => {
    for (const r of records ?? []) {
      mutated = true;
      if (r.type === "childList") reshaped.add(r.target);
    }
  };

  // The observer watches the SOURCE only; our own writes go to the clone, so
  // the walk cannot trip its own alarm.
  const view0 = viewOf(src);
  const MO = view0?.MutationObserver;
  let observer: MutationObserver | null = null;
  if (MO) {
    try {
      observer = new MO(absorb);
      // childList ONLY, deliberately. What this walk can get WRONG is pairing,
      // and only a changed child list breaks that. An attribute or text change
      // mid-walk misattributes nothing — every style still belongs to the node
      // it is written on — it just means the picture was taken of a page that
      // moved, as any photograph is. Observing those too would put the warning
      // on every capture of a page with a clock or a spinner in it, and a
      // warning that fires always carries no information.
      observer.observe(src, { subtree: true, childList: true });
    } catch {
      observer = null; // no observer is a loss of precision, not of correctness
    }
  }

  // Records are drained at every yield rather than left to the observer's own
  // callback: a mutation can only happen while we are yielded (this is one
  // thread), so draining on the way back in is what guarantees the reshaped set
  // is complete BEFORE any further pairing is done with it.
  const drain = (): void => {
    if (!observer) return;
    try {
      absorb(observer.takeRecords());
    } catch {
      /* frame gone */
    }
  };

  try {
    const queue: Array<[Element, Element]> = [[src, dst]];
    while (queue.length) {
      if (styled >= maxElements) {
        stopped = stopped || "elements";
        break;
      }
      if (Date.now() > deadline) {
        stopped = stopped || "deadline";
        break;
      }
      const pair = queue.shift();
      if (!pair) break;
      const [s, d] = pair;
      // Re-checked per node, not once up front: the whole point is that this
      // can change between chunks.
      const view = viewOf(s);
      if (!view) {
        detached = true;
        continue;
      }
      const cs = view.getComputedStyle(s);
      let css = "";
      for (let i = 0; i < cs.length; i++) {
        const prop = cs[i];
        css += prop + ":" + cs.getPropertyValue(prop) + ";";
      }
      d.setAttribute("style", css);
      // How far the user has scrolled THIS box. Not for the root: `src` is the
      // app's <body>, whose scroll offset is the WINDOW's, and `domShot`
      // already shifts the whole clone by that — recording it here too would
      // scroll the capture twice and land it below what was on screen.
      if (s !== src && (s.scrollTop || s.scrollLeft)) {
        scrolled.push({ clone: d, x: s.scrollLeft || 0, y: s.scrollTop || 0 });
      }
      const a = s.children;
      const b = d.children;
      if (reshaped.has(s) || a.length !== b.length) {
        // This parent's children have moved since the clone was taken, so
        // `a[i]` and `b[i]` are no longer the same element. Dropping the
        // subtree costs its styling; pairing it anyway would put one element's
        // appearance on another and call it a photograph.
        mutated = true;
      } else {
        for (let i = 0; i < a.length; i++) {
          const sc = a[i];
          const dc = b[i];
          if (sc && dc) queue.push([sc, dc]);
        }
      }
      if (++styled % STYLE_CHUNK === 0) {
        await new Promise((r) => setTimeout(r, 0));
        drain();
      }
    }
  } finally {
    // One last drain: the callback is a microtask, so a mutation during the
    // final chunk may not have been delivered anywhere else.
    drain();
    if (observer) {
      try {
        observer.disconnect();
      } catch {
        /* gone */
      }
    }
  }

  const incomplete: Incomplete = mutated ? "mutated" : detached ? "detached" : stopped;
  return { styled, incomplete, scrolled };
}

// Put every scrolled box back where the user had it.
//
// THE BUG THIS EXISTS FOR: `cloneNode` copies ATTRIBUTES, and
// `scrollTop`/`scrollLeft` are PROPERTIES. There is no markup for "scrolled
// 3160px down" — so a clone of a scrolled page is a clone of that page at the
// TOP, and there is nothing in the serialized SVG that could say otherwise.
// Compensating for the WINDOW's scroll alone is correct for a document that
// scrolls itself and wrong for every app whose content lives in an inner
// `overflow: auto` box — which is most of them.
//
// The fix has to be expressible in markup, so it is a transform on the CHILDREN
// rather than a scroll offset on the parent: the scroll box keeps the
// `overflow` its computed style already carries (which is what clips), and each
// child is shifted up/left by the offset — exactly the paint the browser does
// when it scrolls, and it changes no layout, so a flex or grid scroller is not
// rearranged by being photographed.
//
// `position: fixed` and `position: sticky` children are SKIPPED, and shifting
// them would be wrong: a sticky header does not move with the scroll, which is
// the whole point of it, and an unshifted clone already draws it where a stuck
// one sits.
//
// The child's own transform is preserved and COMPOSED with, never replaced: the
// style walk writes the computed `transform` (a matrix, for anything the app
// has transformed itself), and dropping it would flatten a rotated or scaled
// element while fixing its scroll. Ours goes FIRST — leftmost is outermost — so
// the shift is applied in the scroller's space, after the element's own
// transform, which is what scrolling does.
//
// The last declaration in a style attribute wins, so this APPENDS rather than
// rewriting the property in place: same result, and no regex has to find and
// splice a value that may contain semicolons inside a `matrix(...)`.
export function applyScrollOffsets(scrolled: ScrolledBox[] | undefined): number {
  let shifted = 0;
  for (const box of scrolled ?? []) {
    for (const child of Array.from(box.clone.children)) {
      const style = child.getAttribute("style") ?? "";
      const pos = /(?:^|;)\s*position\s*:\s*([a-z-]+)/.exec(style);
      if (pos && (pos[1] === "fixed" || pos[1] === "sticky")) continue;
      const own = /(?:^|;)\s*transform\s*:\s*([^;]+)/.exec(style);
      const keep = own && own[1].trim() !== "none" ? " " + own[1].trim() : "";
      child.setAttribute(
        "style",
        style + ";transform:translate(" + -box.x + "px," + -box.y + "px)" + keep + ";",
      );
      shifted++;
    }
  }
  return shifted;
}

// Every `url(...)` a style attribute points at, minus the two that are already
// local: `data:` (nothing to do) and `url(#id)` (an SVG fragment reference — a
// filter or clip path in the same document, which travels with the clone).
export function styleUrls(style: string): string[] {
  const out: string[] = [];
  const re = /url\((['"]?)([^'")]+)\1\)/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(style))) {
    const u = (m[2] ?? "").trim();
    if (u && u.slice(0, 5) !== "data:" && u[0] !== "#") out.push(u);
  }
  return out;
}

// What a picture that could not be fetched leaves behind: a dashed box the size
// of the image, saying so. NOT a broken-image glyph (which reads as a bug in
// the page being photographed rather than in the photograph) and NOT nothing at
// all (which would silently redraw the layout around a hole the user's screen
// did not have). The alt text goes in because it is the one description of the
// missing picture that the page itself wrote.
export function imagePlaceholder(d: Element, alt: string): void {
  const doc = d.ownerDocument;
  if (!doc) return;
  const box = doc.createElement("div");
  box.setAttribute(
    "style",
    (d.getAttribute("style") ?? "") +
      ";display:flex;align-items:center;justify-content:center;text-align:center;" +
      "box-sizing:border-box;overflow:hidden;border:1px dashed #b4b4b4;" +
      "color:#8a8a8a;font:11px system-ui,sans-serif;",
  );
  box.textContent = alt ? "image not captured — " + alt : "image not captured";
  d.replaceWith(box);
}

function blobToDataUrl(blob: Blob): Promise<string> {
  return new Promise((res, rej) => {
    const fr = new FileReader();
    fr.onload = () => res(String(fr.result ?? ""));
    fr.onerror = () => rej(new Error("the image bytes could not be read"));
    fr.readAsDataURL(blob);
  });
}

function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((res, rej) => {
    const img = new Image();
    img.onload = () => res(img);
    img.onerror = () => rej(new Error("the image could not be decoded"));
    img.src = src;
  });
}

// Longest edge down to `maxEdge`, never UP (a 320px button blown up is more
// bytes and no more information). Edges floor at 1px, because a zero-dimension
// canvas throws on `toBlob`.
export function fitWithin(
  w: number,
  h: number,
  maxEdge: number,
): { width: number; height: number; scale: number } {
  const scale = Math.min(1, maxEdge / Math.max(w, h));
  return {
    width: Math.max(1, Math.round(w * scale)),
    height: Math.max(1, Math.round(h * scale)),
    scale,
  };
}

function canvasToBlob(canvas: HTMLCanvasElement, type: string): Promise<Blob | null> {
  return new Promise((res) => canvas.toBlob(res, type));
}

// Re-encode an oversized image down to something worth embedding.
//
// Drawn from an OBJECT URL rather than from the page's own `<img>`: a blob URL
// is same-origin, so the canvas is never tainted and `toDataURL` cannot throw
// for a cross-origin image the way reading the live element would.
async function shrinkImage(blob: Blob, hostDocument: Document): Promise<Blob> {
  const objUrl = URL.createObjectURL(blob);
  try {
    const img = await loadImage(objUrl);
    const fit = fitWithin(img.naturalWidth || 1, img.naturalHeight || 1, IMG_MAX_EDGE);
    const c = hostDocument.createElement("canvas");
    c.width = fit.width;
    c.height = fit.height;
    const ctx = c.getContext("2d");
    if (!ctx) return blob;
    ctx.drawImage(img, 0, 0, fit.width, fit.height);
    return (await canvasToBlob(c, "image/png")) ?? blob;
  } finally {
    URL.revokeObjectURL(objUrl);
  }
}

// One URL's bytes as a `data:` URL. `el` is the element that referenced it, and
// it is only ever the SECOND chance: a cross-origin image served without CORS
// headers cannot be fetched, but the browser has already loaded it into the
// element, and drawing that element into a canvas gets the pixels back — unless
// it TAINTS the canvas, which is precisely the case `toDataURL` throws for. So
// the order is fetch (works for our own routes, and for any host that sends
// `access-control-allow-origin`), then the element, then nothing.
async function urlAsData(
  url: string,
  el: HTMLImageElement | null,
  hostDocument: Document,
): Promise<string> {
  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error("HTTP " + res.status);
    const blob = await res.blob();
    const small = blob.size > IMG_MAX_BYTES ? await shrinkImage(blob, hostDocument) : blob;
    return await blobToDataUrl(small);
  } catch (err) {
    if (!el || !el.naturalWidth) throw err;
    const c = hostDocument.createElement("canvas");
    c.width = el.naturalWidth;
    c.height = el.naturalHeight;
    const ctx = c.getContext("2d");
    if (!ctx) throw err;
    ctx.drawImage(el, 0, 0);
    return c.toDataURL("image/png"); // throws when the canvas is tainted
  }
}

// Rewrite every image reference in the clone to `data:`, and return how many
// could NOT be — the SVG is loaded as a data: URL and cannot reach out, so an
// un-inlined reference is simply a hole.
//
// Pairing is by INDEX here, not by the style walk's guarded descent, and that
// is a deliberate difference in kind: a mispaired image swaps one picture for
// another rather than dressing an element in a stranger's layout, and the walk
// has already reported `mutated` for any capture where the pairing could have
// slipped.
//
// `<source>` elements go FIRST. A `<picture>` whose `<source>` still points at
// an http URL would have the browser re-resolve the child over the `src` we
// just rewrote, and lose the image again inside the very element we fixed.
export async function inlineImages(
  src: Element,
  dst: Element,
  deadline: number,
  hostDocument: Document,
): Promise<{ missing: number }> {
  let missing = 0;
  let fetched = 0;
  const cache = new Map<string, Promise<string>>();

  const resolve = (url: string, el: HTMLImageElement | null): Promise<string> => {
    let hit = cache.get(url);
    if (!hit) {
      fetched++;
      hit = urlAsData(url, el, hostDocument);
      cache.set(url, hit);
    }
    return hit;
  };
  // Room for one more DISTINCT url: an image repeated ten times costs one
  // fetch. The deadline is the image TIMEOUT — past it, the rest are holes
  // rather than an unbounded wait.
  const room = (url: string): boolean =>
    cache.has(url) || (fetched < IMG_MAX && Date.now() < deadline);

  try {
    for (const s of Array.from(dst.querySelectorAll("picture source"))) s.remove();
  } catch {
    /* a clone with no <picture> in it */
  }

  const ss = src.querySelectorAll("img");
  const ds = dst.querySelectorAll("img");
  for (let i = 0; i < ss.length && i < ds.length; i++) {
    const s = ss[i];
    const d = ds[i];
    if (!s || !d) continue;
    // currentSrc, not src: it is what the browser actually PICKED out of a
    // srcset, which is the picture on screen and the one being photographed.
    const url = s.currentSrc || s.getAttribute("src") || "";
    if (!url || url.slice(0, 5) === "data:") continue;
    if (!room(url)) {
      missing++;
      imagePlaceholder(d, s.alt);
      continue;
    }
    try {
      d.setAttribute("src", await resolve(url, s));
      // Both would re-select a URL over the src we just wrote.
      d.removeAttribute("srcset");
      d.removeAttribute("sizes");
    } catch {
      missing++;
      imagePlaceholder(d, s.alt);
    }
  }

  // Backgrounds, masks, borders and list markers: the style walk has already
  // written the computed value onto every clone element, so this reads them
  // back off the CLONE and needs no second pass over the live tree. Scanned by
  // substring first — `url(` appears in a handful of the thousands of style
  // attributes here, and the regex is not worth running on the rest.
  for (const el of Array.from(dst.querySelectorAll("*"))) {
    const style = el.getAttribute("style") ?? "";
    if (style.indexOf("url(") === -1) continue;
    let out = style;
    for (const url of styleUrls(style)) {
      if (!room(url)) {
        missing++;
        continue;
      }
      try {
        const data = await resolve(url, null);
        out = out.split(url).join(data);
      } catch {
        // No placeholder for a background: the element keeps its size and its
        // colour, and the missing layer is counted. Substituting a dashed box
        // for a texture would be a bigger lie than leaving it plain.
        missing++;
      }
    }
    if (out !== style) el.setAttribute("style", out);
  }

  return { missing };
}

// A readback equal to a same-size blank canvas is a blank readback — which is
// what a WebGL context created with `preserveDrawingBuffer: false` gives.
export function readbackIsBlank(url: string, blankUrl: string): boolean {
  return !url || !blankUrl || url === blankUrl;
}

// Replace each `<canvas>` in the clone with an `<img>` of its own pixels: a
// `<canvas>` serializes as an EMPTY element, so without this every chart and
// map in the capture is a hole. Returns how many source canvases could NOT be
// read back (tainted, or a WebGL buffer that was not preserved) — those are
// left as `<canvas>` in the clone, which serializes empty: the same hole they
// would have been anyway.
export function rasteriseCanvases(src: Element, dst: Element): number {
  const cs = src.querySelectorAll("canvas");
  const cd = dst.querySelectorAll("canvas");
  const doc = src.ownerDocument;
  const dstDoc = dst.ownerDocument;
  if (!doc || !dstDoc) return 0;
  const probe = doc.createElement("canvas");
  let blanks = 0;
  for (let i = 0; i < cs.length && i < cd.length; i++) {
    const s = cs[i];
    const d = cd[i];
    if (!s || !d) continue;
    let url = "";
    try {
      url = s.toDataURL("image/png");
    } catch {
      url = ""; // tainted
    }
    let blankUrl = "";
    try {
      probe.width = s.width;
      probe.height = s.height;
      blankUrl = probe.toDataURL("image/png");
    } catch {
      blankUrl = "";
    }
    if (readbackIsBlank(url, blankUrl)) {
      blanks++;
      continue;
    }
    const img = dstDoc.createElement("img");
    img.setAttribute("src", url);
    img.setAttribute("style", d.getAttribute("style") ?? "");
    d.replaceWith(img);
  }
  return blanks;
}

// The app's own page background, from `<html>` then `<body>`. Returns "" when
// both are transparent, which the caller reads as "use white". A foreignObject
// paints NOTHING where the page is transparent, and `<html>`'s background does
// not come along with `<body>`'s clone — so without a backdrop a light app
// rasterises as BLACK once the PNG is flattened.
export function backdropColor(win: Window): string {
  const opaque = (el: Element | null): string => {
    if (!el) return "";
    const bg = win.getComputedStyle(el).backgroundColor;
    return bg && bg !== "transparent" && !/^rgba\(.*,\s*0\)$/.test(bg) ? bg : "";
  };
  try {
    return opaque(win.document.documentElement) || opaque(win.document.body);
  } catch {
    return "";
  }
}

// Whether there is anything in this body worth photographing. An empty body is
// a frame that has not painted (or an error page), and a picture of it baked
// into an artifact is a permanently wrong thumbnail nothing downstream can
// catch.
function isEmptyBody(body: HTMLElement): boolean {
  return body.childElementCount === 0 && !(body.textContent ?? "").trim();
}

export interface DomShotOptions {
  // The capture box in CSS pixels. Defaults to the document's own client size,
  // which is what a caller wants when the frame IS the viewport.
  width?: number;
  height?: number;
  // Device pixels per CSS pixel — the canvas is this much larger than the
  // capture box, so a 2x display gets a 2x picture.
  scale?: number;
  // A `Date.now()` stamp the whole capture stops at.
  deadline?: number;
  // The document whose `createElement` makes the output canvas: the SHELL's,
  // since the canvas is drawn and encoded here, not in the app's frame.
  hostDocument?: Document;
}

export interface DomShotResult {
  canvas: HTMLCanvasElement;
  // Device pixels.
  width: number;
  height: number;
  // How many elements got their computed style, and what (if anything) stopped
  // the capture from being whole.
  styled: number;
  incomplete: Incomplete;
  // Canvases whose pixels could not be read back — the WebGL cost in the module
  // header. Each is a hole in the picture.
  blanks: number;
  // Image references that could not be inlined.
  imagesMissing: number;
}

// Rasterise `win`'s document by cloning its DOM. Returns null when there is no
// readable, non-empty body to capture — the caller's cue to ship without a
// picture rather than with a wrong one. THROWS only when the serialized markup
// cannot be rasterised, which callers treat the same way.
export async function domShot(
  win: Window,
  opts: DomShotOptions = {},
): Promise<DomShotResult | null> {
  const doc = win.document;
  const body: HTMLElement | null = doc ? doc.body : null;
  if (!body || isEmptyBody(body)) return null;
  const root = doc.documentElement;

  const hostDocument = opts.hostDocument ?? document;
  const deadline = opts.deadline ?? Date.now() + SHOT_TIMEOUT_MS;
  const scale = opts.scale && opts.scale > 0 ? opts.scale : 1;
  const w = Math.max(1, Math.round(opts.width ?? root.clientWidth ?? body.offsetWidth));
  const h = Math.max(1, Math.round(opts.height ?? root.clientHeight ?? body.offsetHeight));

  const clone = body.cloneNode(true) as HTMLElement;
  // The style walk stops SHORT of the deadline so the image fetches have a tail
  // to run in (see IMG_TAIL_MS).
  const styles = await inlineComputedStyles(
    body,
    clone,
    Math.max(Date.now(), deadline - IMG_TAIL_MS),
  );
  // BEFORE rasteriseCanvases, which puts data:-URL `<img>`s of its own into the
  // clone: running after it would pair those against the source's real images
  // by index and put a chart's pixels where a logo was.
  const images = await inlineImages(body, clone, deadline, hostDocument);
  const blanks = rasteriseCanvases(body, clone);
  // LAST, so a canvas swapped for an `<img>` above is shifted with the rest of
  // its scroll box rather than left at the top of it.
  applyScrollOffsets(styles.scrolled);

  // Shift the clone by the app's WINDOW scroll so the picture matches what was
  // on screen; every inner `overflow: auto` box was already put back by
  // applyScrollOffsets.
  const sx = Math.round(win.scrollX || 0);
  const sy = Math.round(win.scrollY || 0);
  const xml = new XMLSerializer().serializeToString(clone);
  const svg =
    '<svg xmlns="http://www.w3.org/2000/svg" width="' + w + '" height="' + h + '">' +
    '<foreignObject width="100%" height="100%">' +
    '<div xmlns="http://www.w3.org/1999/xhtml" style="transform:translate(' +
    -sx +
    "px," +
    -sy +
    'px)">' +
    xml +
    "</div>" +
    "</foreignObject></svg>";

  const img = new Image();
  await new Promise<void>((res, rej) => {
    img.onload = () => res();
    // An `<img>` load failure carries no reason, so say what it MEANS instead
    // of reporting an empty event: the serialized markup was not valid XHTML.
    img.onerror = () => rej(new Error("the app's markup could not be rasterised"));
    img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
  });

  const cw = Math.max(1, Math.round(w * scale));
  const ch = Math.max(1, Math.round(h * scale));
  const canvas = hostDocument.createElement("canvas");
  canvas.width = cw;
  canvas.height = ch;
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;
  ctx.fillStyle = backdropColor(win) || "#ffffff";
  ctx.fillRect(0, 0, cw, ch);
  ctx.drawImage(img, 0, 0, cw, ch);

  return {
    canvas,
    width: cw,
    height: ch,
    styled: styles.styled,
    incomplete: styles.incomplete,
    blanks,
    imagesMissing: images.missing,
  };
}
