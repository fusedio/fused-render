// THE DOM-CLONE CAPTURE (T:9044-10022) — the last of the three paths, and the
// only one that needs a readable document.
//
// The clone is serialized into an `<svg>` and loaded through an `<img>`, which
// renders with external resource loading DISABLED — no network, of any kind, to
// any origin, same-origin included. So every rule has to be inlined
// (`inlineStyles`), every picture fetched and rewritten to `data:`
// (`inlineImages`), every canvas rasterised (`rasterise`) and every scrolled box
// put back by hand (`applyScroll`) before the bytes leave.
//
// Same-origin DIRECT access, guarded exactly as T's is: the framed document can
// be mid-navigation, absent or cross-origin, and a failure degrades to a
// caveated picture, never to a throw.
import { cropRect, dataUrl, rasterise, shrinkImage } from "./encode";
import {
  SHOT_IMG_MAX,
  SHOT_IMG_MAX_BYTES,
  SHOT_IMG_MS,
  SHOT_MAX_ELEMENTS,
  SHOT_STYLE_CHUNK,
  type PaneBitmap,
} from "./types";

export interface StyleWalk {
  styled: number;
  /** One cause, never a set of booleans that can disagree — the caller turns it
   *  into ONE sentence. Precedence when several apply: a re-render may have made
   *  styles WRONG anywhere, while a cap only leaves them MISSING past a point, so
   *  correctness news outranks budget news (T:9110, 9215). */
  incomplete: "" | "elements" | "deadline" | "detached" | "mutated";
  scrolled: { clone: Element; x: number; y: number }[];
}

/** `<style>` rules do not travel with a serialized element, so every computed
 *  property is copied onto the clone's inline style — this is what makes the
 *  capture look like the app instead of like unstyled HTML.
 *
 *  It is also the expensive part: one getComputedStyle plus ~340 longhand reads
 *  PER ELEMENT. So it YIELDS every SHOT_STYLE_CHUNK elements (which is what makes
 *  the budget's timer able to fire at all — a timer cannot fire while synchronous
 *  code runs) and STOPS at SHOT_MAX_ELEMENTS or past `deadline`.
 *
 *  Breadth-first BECAUSE it can stop early: the elements nearest `<body>` are the
 *  layout containers, so a truncated capture still has the page's overall shape.
 *
 *  And the price of yielding is that the source is LIVE: between chunks the app
 *  can re-render, after which `s.children`/`d.children` stop corresponding and
 *  every style past that point lands on the WRONG element. Detected two ways
 *  (a childList MutationObserver on the source, plus a child-count compare) and
 *  REPORTED, never retried: a missing style degrades a picture, a wrong one lies
 *  about it (T:9044-9218). */
export async function inlineStyles(
  src: Element,
  dst: Element,
  deadline: number,
): Promise<StyleWalk> {
  let styled = 0;
  let stopped: StyleWalk["incomplete"] = "";
  let detached = false;
  let mutated = false;
  const scrolled: StyleWalk["scrolled"] = [];
  const reshaped = new Set<Node>();
  const absorb = (records: MutationRecord[] | null | undefined): void => {
    for (const r of records || []) {
      mutated = true;
      if (r.type === "childList") reshaped.add(r.target);
    }
  };
  // The observer watches the SOURCE only; our own writes go to the clone, so the
  // walk cannot trip its own alarm.
  const view0 = src.ownerDocument && src.ownerDocument.defaultView;
  const MO = view0 && view0.MutationObserver;
  let observer: MutationObserver | null = null;
  if (MO) {
    try {
      observer = new MO(absorb);
      // childList ONLY, deliberately: what this walk can get WRONG is pairing,
      // and only a changed child list breaks that. Observing attributes would put
      // the distrust note on every capture of a page with a clock in it, and a
      // warning that fires always carries no information (T:9146).
      observer.observe(src, { subtree: true, childList: true });
    } catch {
      observer = null; // no observer is a loss of precision, not of correctness
    }
  }
  // Drained at every yield rather than left to the observer's own callback: a
  // mutation can only happen while we are yielded (this is one thread), so
  // draining on the way back in is what guarantees `reshaped` is complete BEFORE
  // any further pairing is done with it (T:9163).
  const drain = (): void => {
    if (!observer) return;
    try {
      absorb(observer.takeRecords());
    } catch {
      /* frame gone */
    }
  };
  try {
    const queue: [Element, Element][] = [[src, dst]];
    while (queue.length) {
      if (styled >= SHOT_MAX_ELEMENTS) {
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
      // Re-checked per node, not once up front: the whole point is that this can
      // change between chunks.
      const view = s.isConnected ? s.ownerDocument && s.ownerDocument.defaultView : null;
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
      // Not for the root: `src` is the app's <body>, whose scroll offset is the
      // WINDOW's, and the caller already shifts the whole clone by that —
      // recording it here too would scroll the capture twice (T:9186).
      if (s !== src && (s.scrollTop || s.scrollLeft)) {
        scrolled.push({ clone: d, x: s.scrollLeft || 0, y: s.scrollTop || 0 });
      }
      const a = s.children;
      const b = d.children;
      if (reshaped.has(s) || a.length !== b.length) {
        // This parent's children have moved since the clone was taken, so `a[i]`
        // and `b[i]` are no longer the same element. Dropping the subtree costs
        // its styling; pairing it anyway would put one element's appearance on
        // another and call it a photograph.
        mutated = true;
      } else {
        for (let i = 0; i < a.length; i++) queue.push([a[i], b[i]]);
      }
      if (++styled % SHOT_STYLE_CHUNK === 0) {
        await new Promise((r) => setTimeout(r, 0));
        drain();
      }
    }
  } finally {
    // One last drain: the callback is a microtask, so a mutation during the final
    // chunk may not have been delivered anywhere else.
    drain();
    if (observer) {
      try {
        observer.disconnect();
      } catch {
        /* gone */
      }
    }
  }
  const incomplete: StyleWalk["incomplete"] = mutated
    ? "mutated"
    : detached
      ? "detached"
      : stopped;
  return { styled, incomplete, scrolled };
}

/** Put every scrolled box back where the user had it.
 *
 *  `cloneNode` copies ATTRIBUTES, and `scrollTop`/`scrollLeft` are PROPERTIES:
 *  there is no markup for "scrolled 3160px down", so a clone of a scrolled page
 *  is a clone of that page at the top. The fix has to be expressible in markup,
 *  so it is a transform on the CHILDREN rather than an offset on the parent —
 *  exactly the paint the browser does when it scrolls, and it changes no layout.
 *
 *  `fixed`/`sticky` children are skipped (a stuck header does not move with the
 *  scroll), and the child's own transform is COMPOSED with, never replaced —
 *  ours goes first, leftmost is outermost (T:9219-9265). */
export function applyScroll(scrolled: StyleWalk["scrolled"] | null | undefined): number {
  let shifted = 0;
  for (const box of scrolled || []) {
    for (const child of Array.from(box.clone.children)) {
      const style = child.getAttribute("style") || "";
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

/** Every `url(…)` a style attribute points at, minus the two already local:
 *  `data:` (nothing to do) and `url(#id)` (an SVG fragment reference, which
 *  travels with the clone) (T:9451). */
export function styleUrls(style: string): string[] {
  const out: string[] = [];
  const re = /url\((['"]?)([^'")]+)\1\)/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(style))) {
    const u = m[2].trim();
    if (u && u.slice(0, 5) !== "data:" && u[0] !== "#") out.push(u);
  }
  return out;
}

/** What a picture that could not be fetched leaves behind: a dashed box the size
 *  of the image, saying so. NOT a broken-image glyph (which reads as a bug in the
 *  page being photographed) and NOT nothing at all (which would silently redraw
 *  the layout around a hole the user's screen did not have) (T:9466). */
export function imagePlaceholder(d: Element, alt: string | null | undefined): void {
  const box = d.ownerDocument.createElement("div");
  box.setAttribute(
    "style",
    (d.getAttribute("style") || "") +
      ";display:flex;align-items:center;justify-content:center;text-align:center;" +
      "box-sizing:border-box;overflow:hidden;border:1px dashed #b4b4b4;" +
      "color:#8a8a8a;font:11px system-ui,sans-serif;",
  );
  box.textContent = alt ? "image not captured — " + alt : "image not captured";
  d.replaceWith(box);
}

/** One URL's bytes as a `data:` URL. `el` is only ever the SECOND chance: a
 *  cross-origin image served without CORS headers cannot be fetched, but the
 *  browser has already loaded it into the element, and drawing that element into
 *  a canvas gets the pixels back — unless it taints the canvas, which is
 *  precisely what `toDataURL` throws for (T:9430). */
export async function urlAsData(url: string, el: HTMLImageElement | null): Promise<string> {
  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error("HTTP " + res.status);
    const blob = await res.blob();
    const small = blob.size > SHOT_IMG_MAX_BYTES ? await shrinkImage(blob) : blob;
    return await dataUrl(small);
  } catch (err) {
    if (!el || !el.naturalWidth) throw err;
    const c = el.ownerDocument.createElement("canvas");
    c.width = el.naturalWidth;
    c.height = el.naturalHeight;
    c.getContext("2d")?.drawImage(el, 0, 0);
    return c.toDataURL("image/png"); // throws when the canvas is tainted
  }
}

/** Rewrite every image reference in the clone to `data:`, and report how many
 *  could not be. Pairing is by INDEX here, deliberately unlike the style walk's
 *  guarded descent: a mispaired image swaps one picture for another rather than
 *  dressing an element in a stranger's layout, and the walk has already reported
 *  `mutated` for any capture where the pairing could have slipped.
 *
 *  `<source>` elements go first: a `<picture>` whose `<source>` still points at
 *  an http URL would have the browser re-resolve the child over the `src` we just
 *  rewrote (T:9491-9552). */
export async function inlineImages(
  src: Element,
  dst: Element,
  deadline: number,
): Promise<{ missing: number }> {
  let missing = 0;
  let fetched = 0;
  const cache = new Map<string, Promise<string>>();
  const resolve = (url: string, el: HTMLImageElement | null): Promise<string> => {
    let hit = cache.get(url);
    if (!hit) {
      fetched++;
      hit = urlAsData(url, el);
      cache.set(url, hit);
    }
    return hit;
  };
  // Room for one more DISTINCT url: an image repeated ten times costs one fetch.
  const room = (url: string): boolean =>
    cache.has(url) || (fetched < SHOT_IMG_MAX && Date.now() < deadline);
  try {
    for (const s of Array.from(dst.querySelectorAll("picture source"))) s.remove();
  } catch {
    /* a clone with no <picture> in it */
  }
  const ss = src.querySelectorAll("img");
  const ds = dst.querySelectorAll("img");
  for (let i = 0; i < ss.length && i < ds.length; i++) {
    // currentSrc, not src: it is what the browser actually PICKED out of a
    // srcset, which is the picture on screen and the one being photographed.
    const url = ss[i].currentSrc || ss[i].getAttribute("src") || "";
    if (!url || url.slice(0, 5) === "data:") continue;
    if (!room(url)) {
      missing++;
      imagePlaceholder(ds[i], ss[i].alt);
      continue;
    }
    try {
      ds[i].setAttribute("src", await resolve(url, ss[i]));
      // Both would re-select a URL over the src we just wrote.
      ds[i].removeAttribute("srcset");
      ds[i].removeAttribute("sizes");
    } catch {
      missing++;
      imagePlaceholder(ds[i], ss[i].alt);
    }
  }
  // Backgrounds, masks, borders and list markers: the style walk has already
  // written the computed value onto every clone element, so this reads them back
  // off the CLONE and needs no second pass over the live tree.
  for (const el of Array.from(dst.querySelectorAll("*"))) {
    const style = el.getAttribute("style") || "";
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
        // colour, and the missing layer is counted in the note. Substituting a
        // dashed box for a texture would be a bigger lie than leaving it plain.
        missing++;
      }
    }
    if (out !== style) el.setAttribute("style", out);
  }
  return { missing };
}

// ── the caveats (T:9275-9345, 10052) ────────────────────────────────────────

/** The one sentence a shot carries about what its capture could NOT do, or "".
 *  Every cause `inlineStyles` can report is worded here and nowhere else
 *  (T:9275). */
export function paneNote(pane: Pick<PaneBitmap, "styled" | "incomplete"> | null): string {
  const styled = pane?.styled || 0;
  switch (pane?.incomplete) {
    case "elements":
      return (
        "part of this capture is unstyled: the page has more elements than " +
        "the capture budget allows (it stopped after " +
        styled +
        "), so some of " +
        "what you see may render without its CSS rather than as the user saw it"
      );
    case "deadline":
      return (
        "part of this capture is unstyled: it ran out of time after " +
        styled +
        " elements, so some of what you see may render without its CSS rather " +
        "than as the user saw it. This is about capture speed, not page size"
      );
    case "detached":
      return (
        "part of this capture is unstyled: the page removed elements while the " +
        "capture was running, so some of what you see may render without its CSS"
      );
    case "mutated":
      return (
        "the page re-rendered while the capture was running, so this picture " +
        "may not match what was on screen: some elements are missing their " +
        "styling and the layout may be a blend of before and after"
      );
    default:
      return "";
  }
}

/** The other caveat, deliberately NOT one of `paneNote`'s causes: those are all
 *  "some of this may not be what you think it is" over an unknown set of
 *  elements, while this one is BOUNDED and visible in the image itself (T:9304). */
export function imageNote(pane: Pick<PaneBitmap, "imagesMissing"> | null): string {
  const n = pane?.imagesMissing || 0;
  if (!n) return "";
  return (
    n +
    (n === 1 ? " image" : " images") +
    " could not be embedded in this " +
    "picture and " +
    (n === 1 ? "shows" : "show") +
    " as a dashed " +
    '"image not captured" box (or, for a background, as plain colour): a page ' +
    "rasterised through SVG cannot load a URL, so every image has to be fetched " +
    "and inlined first, and " +
    (n === 1 ? "that one" : "those") +
    " could not " +
    "be. The app is very likely showing the image fine"
  );
}

/** The closing instruction a caveated shot carries: what to DO about the doubt.
 *  Chosen by the WORST doubt present — bounded doubt keeps the reassurance,
 *  unbounded doubt replaces it with corroboration (T:9332). */
export function trustLine(incomplete: StyleWalk["incomplete"] | false | undefined): string {
  return incomplete
    ? "Do not act on this image alone — check anything you read from it against " +
        "the element's anchor and the DOM outline first."
    : "The rest of the image is what the user saw.";
}

/** Where the blank WebGL regions ARE, in pane-bitmap coordinates, as prose. A
 *  crop can be suppressed because the other crops survive it; a pane shot cannot
 *  — suppressing it leaves nothing at all — so it ships WITH a note saying which
 *  rectangles are the app's backdrop rather than what was drawn (T:10052).
 *
 *  `rectOf` is the element's rect in pane-bitmap space; the default is
 *  `getBoundingClientRect`, which for a same-origin framed document IS that
 *  space. PR3's `annStageRect` is passed in where the pane is the host's. */
export function blankRegions(
  pane: Pick<PaneBitmap, "blanks" | "width" | "height"> | null,
  rectOf: (el: Element) => { left: number; top: number; width: number; height: number } = (el) =>
    el.getBoundingClientRect(),
): string {
  const rects: string[] = [];
  for (const cv of pane?.blanks || []) {
    const r = cropRect(rectOf(cv), pane?.width || 0, pane?.height || 0);
    if (r) {
      rects.push(
        Math.round(r.width) +
          "x" +
          Math.round(r.height) +
          " at (" +
          Math.round(r.left) +
          "," +
          Math.round(r.top) +
          ")",
      );
    }
  }
  if (!rects.length) return "";
  return (
    "the following region" +
    (rects.length === 1 ? " is" : "s are") +
    " a WebGL canvas whose pixels could not be read back, so " +
    (rects.length === 1 ? "it shows" : "they show") +
    " the app's background " +
    "instead of what was drawn there: " +
    rects.join(", ") +
    ". A map/3D library (maplibre, deck.gl) creates its context with " +
    "preserveDrawingBuffer:false, which is why — the app is very likely drawing " +
    "fine, so judge the layout around those regions and not inside them"
  );
}

/** Every caveat a finished capture carries, joined the way the wire's `viewNote`
 *  carries them: "; " between the sentences and the trust line last (T:10111). */
export function caveatsOf(
  pane: PaneBitmap | null,
  rectOf?: (el: Element) => { left: number; top: number; width: number; height: number },
): string[] {
  if (!pane) return [];
  const out: string[] = [];
  const blanks = blankRegions(pane, rectOf);
  if (blanks) out.push(blanks);
  const imgs = imageNote(pane);
  if (imgs) out.push(imgs);
  const note = paneNote(pane);
  if (note) out.push(note);
  return out;
}

export function viewNoteFrom(caveats: string[], incomplete: PaneBitmap["incomplete"]): string {
  return caveats.length ? caveats.join("; ") + ". " + trustLine(incomplete) : "";
}

/** The app's own page background, from `<html>` then `<body>`. "" when both are
 *  transparent, which the caller reads as "use white" (T:10022). */
export function backdrop(win: Window): string {
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

/** Rasterise a readable app document into a bitmap (T:9958's third path).
 *
 *  The clone is styled from the LIVE tree, which is what creates the race
 *  `inlineStyles` defends against: a detached element has no cascade, so
 *  `getComputedStyle` on one enumerates ZERO properties — and putting the clone
 *  INTO the app's document would double the DOM, duplicate every id and mutate
 *  the page the user is looking at. So the live tree is the only source of truth
 *  and the race is detected rather than avoided. */
export async function captureDom(win: Window, deadline: number): Promise<PaneBitmap | null> {
  const doc = win && win.document;
  const body = doc && doc.body;
  if (!body) return null;
  const root = doc.documentElement;
  const w = Math.max(1, Math.round(root.clientWidth || body.offsetWidth));
  const h = Math.max(1, Math.round(root.clientHeight || body.offsetHeight));
  const clone = body.cloneNode(true) as Element;
  // The style walk stops SHORT of the deadline so the image fetches have a tail
  // to run in: sharing one stamp would let a big page spend the entire budget and
  // leave every picture inside it a placeholder (T:9970).
  const styles = await inlineStyles(body, clone, Math.max(Date.now(), deadline - SHOT_IMG_MS));
  // BEFORE `rasterise`, which puts data:-URL <img>s of its own into the clone:
  // running after it would pair those against the source's real images by index
  // and put a chart's pixels where a logo was (T:9976).
  const images = await inlineImages(body, clone, deadline);
  const blanks = rasterise(body, clone);
  // LAST, so a canvas swapped for an <img> is shifted with the rest of its
  // scroll box rather than left at the top of it (T:9981).
  applyScroll(styles.scrolled);
  // The WINDOW's scroll only; every inner `overflow:auto` box was put back above.
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
    // An <img> load failure carries no reason, so say what it MEANS instead of
    // reporting an empty event: the markup was not valid XHTML (T:9999).
    img.onerror = () => rej(new Error("the pane's markup could not be rasterised"));
    img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
  });
  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d");
  // A foreignObject paints nothing where the page is transparent, and <html>'s
  // background does not come along with <body>'s clone — without a backdrop a
  // light app crops as black once the PNG is flattened (T:10009).
  if (ctx) {
    ctx.fillStyle = backdrop(win) || "#ffffff";
    ctx.fillRect(0, 0, w, h);
    ctx.drawImage(img, 0, 0);
  }
  return {
    canvas,
    width: w,
    height: h,
    blanks,
    styled: styles.styled,
    incomplete: styles.incomplete,
    imagesMissing: images.missing,
  };
}
