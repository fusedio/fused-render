// domShot's guards, driven for real against a hand-built fake DOM.
//
// `bun test` has no DOM and the repo carries no happy-dom/jsdom, so the fake
// below implements exactly the surface these functions touch — children,
// attributes, isConnected, ownerDocument.defaultView.getComputedStyle,
// scrollTop/scrollLeft. That is enough to EXECUTE the interesting half of the
// module (the style walk's pairing guards and the scroll compensation) rather
// than only assert on its source, which is what the guards deserve: each one
// exists for a measured wrong picture, not a hypothetical one.
//
// `domShot` itself is not called here — it needs XMLSerializer, an <img> that
// decodes an SVG data URL and a 2D canvas, none of which a fake can honestly
// stand in for. Its ORDERING is pinned by source assertion at the bottom,
// because the order is the part that silently corrupts a picture when changed.
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { expect, test } from "bun:test";

import {
  applyScrollOffsets,
  backdropColor,
  fitWithin,
  imagePlaceholder,
  inlineComputedStyles,
  readbackIsBlank,
  styleUrls,
  type ScrolledBox,
} from "./domShot";

// -- the fake DOM -----------------------------------------------------------

interface FakeDoc {
  defaultView: FakeView | null;
  createElement(tag: string): FakeEl;
}

interface FakeView {
  getComputedStyle(el: FakeEl): FakeStyle;
  MutationObserver?: unknown;
}

class FakeStyle {
  constructor(private readonly props: Record<string, string>) {}
  get length(): number {
    return Object.keys(this.props).length;
  }
  // The walk indexes the declaration (`cs[i]`) to enumerate longhand names.
  [i: number]: string;
  getPropertyValue(p: string): string {
    return this.props[p] ?? "";
  }
}

// Indexed access on a class instance needs a Proxy to answer `cs[0]`.
function styleOf(props: Record<string, string>): FakeStyle {
  const names = Object.keys(props);
  const base = new FakeStyle(props);
  return new Proxy(base, {
    get(t, k) {
      if (typeof k === "string" && /^\d+$/.test(k)) return names[Number(k)];
      return Reflect.get(t, k);
    },
  }) as FakeStyle;
}

class FakeEl {
  children: FakeEl[] = [];
  isConnected = true;
  scrollTop = 0;
  scrollLeft = 0;
  computed: Record<string, string> = {};
  textContent = "";
  replacedWith: FakeEl | null = null;
  private attrs = new Map<string, string>();

  constructor(
    public tag: string,
    public ownerDocument: FakeDoc,
  ) {}

  setAttribute(k: string, v: string): void {
    this.attrs.set(k, v);
  }
  getAttribute(k: string): string | null {
    return this.attrs.get(k) ?? null;
  }
  removeAttribute(k: string): void {
    this.attrs.delete(k);
  }
  replaceWith(other: FakeEl): void {
    this.replacedWith = other;
  }
  add(...kids: FakeEl[]): this {
    this.children.push(...kids);
    return this;
  }
}

function makeDoc(): FakeDoc {
  const doc: FakeDoc = {
    defaultView: null,
    createElement: (tag: string) => new FakeEl(tag, doc),
  };
  doc.defaultView = {
    getComputedStyle: (el: FakeEl) => styleOf(el.computed),
  };
  return doc;
}

const el = (doc: FakeDoc, tag = "div", computed: Record<string, string> = {}): FakeEl => {
  const e = new FakeEl(tag, doc);
  e.computed = computed;
  return e;
};

// The module is typed against the real DOM; the fake satisfies the same shape
// structurally, so the cast is confined to these two helpers.
const asEl = (e: FakeEl): Element => e as unknown as Element;
const asBox = (clone: FakeEl, x: number, y: number): ScrolledBox =>
  ({ clone: asEl(clone), x, y }) as ScrolledBox;

// -- inlineComputedStyles ---------------------------------------------------

test("every computed longhand is copied onto the clone's inline style", () => {
  const doc = makeDoc();
  const src = el(doc, "body", { color: "rgb(1, 2, 3)", display: "flex" });
  const dst = el(doc, "body");
  return inlineComputedStyles(asEl(src), asEl(dst), Date.now() + 5000).then((r) => {
    // This is the whole reason the walk exists: <style> rules do not travel
    // with a serialized element, so without this the capture is unstyled HTML.
    expect(dst.getAttribute("style")).toBe("color:rgb(1, 2, 3);display:flex;");
    expect(r.styled).toBe(1);
    expect(r.incomplete).toBe("");
  });
});

test("the walk descends into paired children", async () => {
  const doc = makeDoc();
  const src = el(doc, "body").add(el(doc, "p", { color: "red" }), el(doc, "p", { color: "blue" }));
  const dst = el(doc, "body").add(el(doc, "p"), el(doc, "p"));
  const r = await inlineComputedStyles(asEl(src), asEl(dst), Date.now() + 5000);
  expect(r.styled).toBe(3);
  expect(dst.children[0]!.getAttribute("style")).toBe("color:red;");
  expect(dst.children[1]!.getAttribute("style")).toBe("color:blue;");
});

test("the element cap stops the walk and reports 'elements'", async () => {
  const doc = makeDoc();
  const src = el(doc, "body");
  const dst = el(doc, "body");
  for (let i = 0; i < 10; i++) {
    src.add(el(doc, "p", { color: "red" }));
    dst.add(el(doc, "p"));
  }
  const r = await inlineComputedStyles(asEl(src), asEl(dst), Date.now() + 5000, 4);
  // A huge page costs a PARTLY styled capture, never an unbounded one — which
  // also bounds the serialized SVG, since it grows per styled element.
  expect(r.styled).toBe(4);
  expect(r.incomplete).toBe("elements");
  expect(dst.children[9]!.getAttribute("style")).toBeNull();
});

test("a blown deadline stops the walk and reports 'deadline'", async () => {
  const doc = makeDoc();
  const src = el(doc, "body", { color: "red" });
  const dst = el(doc, "body");
  const r = await inlineComputedStyles(asEl(src), asEl(dst), Date.now() - 1);
  expect(r.styled).toBe(0);
  // Distinct from "elements" on purpose: the old single `truncated` boolean
  // made a capture that merely ran out of TIME report that the DOM was too
  // large — a misdiagnosis an agent might act on by simplifying a small page.
  expect(r.incomplete).toBe("deadline");
});

test("a detached source node is skipped, never styled from nothing", async () => {
  const doc = makeDoc();
  const gone = el(doc, "p", { color: "red" });
  gone.isConnected = false;
  const src = el(doc, "body").add(gone);
  const dst = el(doc, "body").add(el(doc, "p"));
  const r = await inlineComputedStyles(asEl(src), asEl(dst), Date.now() + 5000);
  // getComputedStyle on a detached element enumerates ZERO properties, so
  // reading it would write an authoritative "no styling" onto the clone.
  expect(dst.children[0]!.getAttribute("style")).toBeNull();
  expect(r.incomplete).toBe("detached");
});

test("a child-count mismatch drops the subtree instead of mispairing it", async () => {
  const doc = makeDoc();
  // The live tree re-rendered: it now has two children where the clone has one.
  const src = el(doc, "body").add(el(doc, "p", { color: "red" }), el(doc, "p", { color: "blue" }));
  const dst = el(doc, "body").add(el(doc, "p"));
  const r = await inlineComputedStyles(asEl(src), asEl(dst), Date.now() + 5000);
  // Pairing it anyway would put one element's appearance on another and call it
  // a photograph. Measured before this guard: 375 clone nodes wearing a
  // stranger's computed style, with nothing reported.
  expect(dst.children[0]!.getAttribute("style")).toBeNull();
  expect(r.incomplete).toBe("mutated");
});

test("'mutated' outranks a budget stop — correctness news first", async () => {
  const doc = makeDoc();
  const src = el(doc, "body").add(el(doc, "p"), el(doc, "p"));
  const dst = el(doc, "body").add(el(doc, "p"));
  const r = await inlineComputedStyles(asEl(src), asEl(dst), Date.now() + 5000, 1);
  // Both apply: the cap fired AND the tree had moved. A cap only leaves styles
  // MISSING past a point; a re-render may have made them WRONG anywhere.
  expect(r.incomplete).toBe("mutated");
});

test("scroll offsets are collected for inner boxes but not for the root", async () => {
  const doc = makeDoc();
  const inner = el(doc, "div", { overflow: "auto" });
  inner.scrollTop = 3160;
  inner.scrollLeft = 12;
  const src = el(doc, "body").add(inner);
  src.scrollTop = 500; // the WINDOW's scroll — domShot shifts the whole clone
  const dst = el(doc, "body").add(el(doc, "div"));
  const r = await inlineComputedStyles(asEl(src), asEl(dst), Date.now() + 5000);
  // Recording the root here too would scroll the capture TWICE and land it
  // below what was on screen.
  expect(r.scrolled).toHaveLength(1);
  expect(r.scrolled[0]!.y).toBe(3160);
  expect(r.scrolled[0]!.x).toBe(12);
  expect(r.scrolled[0]!.clone).toBe(asEl(dst.children[0]!));
});

test("the MutationObserver watches childList on the source subtree only", async () => {
  const doc = makeDoc();
  const seen: unknown[] = [];
  class MO {
    observe(target: unknown, opts: unknown): void {
      seen.push({ target, opts });
    }
    takeRecords(): [] {
      return [];
    }
    disconnect(): void {}
  }
  doc.defaultView!.MutationObserver = MO;
  const src = el(doc, "body", { color: "red" });
  const dst = el(doc, "body");
  await inlineComputedStyles(asEl(src), asEl(dst), Date.now() + 5000);
  // childList ONLY: an attribute or text change mid-walk misattributes nothing,
  // and observing those would put the warning on every capture of a page with a
  // clock in it — a warning that fires always carries no information.
  expect(seen).toHaveLength(1);
  expect(seen[0]).toEqual({ target: asEl(src), opts: { subtree: true, childList: true } });
});

// -- applyScrollOffsets -----------------------------------------------------

// THE BUG: cloneNode copies ATTRIBUTES, and scrollTop/scrollLeft are
// PROPERTIES. There is no markup for "scrolled 3160px down", so a clone of a
// scrolled page is a clone of that page at the TOP.
test("each child of a scrolled box is shifted by the offset", () => {
  const doc = makeDoc();
  const box = el(doc, "div");
  const kid = el(doc, "div");
  kid.setAttribute("style", "color:red;");
  box.add(kid);
  expect(applyScrollOffsets([asBox(box, 12, 3160)])).toBe(1);
  // A transform on the CHILDREN, not a scroll offset on the parent: it is
  // expressible in markup, and it changes no layout, so a flex/grid scroller is
  // not rearranged by being photographed.
  expect(kid.getAttribute("style")).toBe("color:red;;transform:translate(-12px,-3160px);");
});

test("fixed and sticky children are not shifted", () => {
  const doc = makeDoc();
  const box = el(doc, "div");
  const stuck = el(doc, "div");
  stuck.setAttribute("style", "position:sticky;top:0;");
  const fixed = el(doc, "div");
  fixed.setAttribute("style", "position:fixed;");
  const normal = el(doc, "div");
  normal.setAttribute("style", "position:static;");
  box.add(stuck, fixed, normal);
  expect(applyScrollOffsets([asBox(box, 0, 100)])).toBe(1);
  // A sticky header does not move with the scroll, which is the whole point of
  // it — an unshifted clone already draws it where a stuck one sits.
  expect(stuck.getAttribute("style")).toBe("position:sticky;top:0;");
  expect(fixed.getAttribute("style")).toBe("position:fixed;");
  expect(normal.getAttribute("style")).toContain("translate(0px,-100px)");
});

test("the child's own transform is composed with, never replaced", () => {
  const doc = makeDoc();
  const box = el(doc, "div");
  const kid = el(doc, "div");
  kid.setAttribute("style", "transform:matrix(1, 0, 0, 1, 5, 5);");
  box.add(kid);
  applyScrollOffsets([asBox(box, 0, 40)]);
  // Dropping it would flatten a rotated or scaled element while fixing its
  // scroll. Ours goes FIRST — leftmost is outermost — so the shift lands in the
  // scroller's space, which is what scrolling does.
  expect(kid.getAttribute("style")).toBe(
    "transform:matrix(1, 0, 0, 1, 5, 5);;transform:translate(0px,-40px) matrix(1, 0, 0, 1, 5, 5);",
  );
});

test("a computed transform of 'none' is not appended back", () => {
  const doc = makeDoc();
  const box = el(doc, "div");
  const kid = el(doc, "div");
  kid.setAttribute("style", "transform:none;");
  box.add(kid);
  applyScrollOffsets([asBox(box, 0, 10)]);
  expect(kid.getAttribute("style")).toBe("transform:none;;transform:translate(0px,-10px);");
});

test("applyScrollOffsets tolerates an absent list", () => {
  expect(applyScrollOffsets(undefined)).toBe(0);
  expect(applyScrollOffsets([])).toBe(0);
});

// -- styleUrls --------------------------------------------------------------

test("styleUrls finds the references that need inlining and skips local ones", () => {
  const urls = styleUrls(
    "background:url(/api/fs/raw?p=a.png);mask:url('b.svg');" +
      "list-style-image:url(\"c.gif\");border-image:url(data:image/png;base64,AAA);" +
      "filter:url(#blur);",
  );
  // data: has nothing to do, and url(#id) is an SVG fragment reference in the
  // same document, which travels with the clone.
  expect(urls).toEqual(["/api/fs/raw?p=a.png", "b.svg", "c.gif"]);
});

test("styleUrls returns nothing for a style with no url()", () => {
  expect(styleUrls("color:red;display:flex;")).toEqual([]);
});

// -- fitWithin --------------------------------------------------------------

test("fitWithin scales the longest edge down but never up", () => {
  expect(fitWithin(3200, 1600, 1600)).toEqual({ width: 1600, height: 800, scale: 0.5 });
  // A 320px button blown up to 1600 is more bytes and no more information.
  expect(fitWithin(320, 200, 1600)).toEqual({ width: 320, height: 200, scale: 1 });
});

test("fitWithin floors an edge at 1px", () => {
  // A zero-dimension canvas throws on toBlob and would cost the whole capture
  // rather than one image.
  const fit = fitWithin(4000, 1, 100);
  expect(fit.width).toBe(100);
  expect(fit.height).toBe(1);
});

// -- readbackIsBlank --------------------------------------------------------

test("a readback equal to a same-size blank canvas is blank", () => {
  // What a WebGL context created with preserveDrawingBuffer:false gives.
  expect(readbackIsBlank("data:image/png;base64,SAME", "data:image/png;base64,SAME")).toBe(true);
  expect(readbackIsBlank("data:image/png;base64,REAL", "data:image/png;base64,BLANK")).toBe(false);
});

test("an unreadable canvas (tainted, empty url) counts as blank", () => {
  expect(readbackIsBlank("", "data:image/png;base64,BLANK")).toBe(true);
  expect(readbackIsBlank("data:image/png;base64,REAL", "")).toBe(true);
});

// -- imagePlaceholder -------------------------------------------------------

test("an un-inlinable image becomes a dashed box carrying its alt text", () => {
  const doc = makeDoc();
  const img = el(doc, "img");
  img.setAttribute("style", "width:80px;height:60px;");
  imagePlaceholder(asEl(img), "a bar chart");
  const box = img.replacedWith!;
  // NOT a broken-image glyph (which reads as a bug in the page being
  // photographed) and NOT nothing at all (which would redraw the layout around
  // a hole the user's screen did not have).
  expect(box.tag).toBe("div");
  expect(box.getAttribute("style")).toContain("width:80px;height:60px;");
  expect(box.getAttribute("style")).toContain("border:1px dashed");
  expect(box.textContent).toBe("image not captured — a bar chart");
});

test("a placeholder for an image with no alt still says so", () => {
  const doc = makeDoc();
  const img = el(doc, "img");
  imagePlaceholder(asEl(img), "");
  expect(img.replacedWith!.textContent).toBe("image not captured");
});

// -- backdropColor ----------------------------------------------------------

// A foreignObject paints NOTHING where the page is transparent, and <html>'s
// background does not come along with <body>'s clone — so without a backdrop a
// light app rasterises as BLACK once the PNG is flattened.
const winWith = (html: string, body: string): Window =>
  ({
    document: { documentElement: { tag: "html" }, body: { tag: "body" } },
    getComputedStyle: (el: { tag: string }) => ({
      backgroundColor: el.tag === "html" ? html : body,
    }),
  }) as unknown as Window;

test("backdropColor prefers <html>, then <body>", () => {
  expect(backdropColor(winWith("rgb(255, 255, 255)", "rgb(1, 1, 1)"))).toBe("rgb(255, 255, 255)");
  expect(backdropColor(winWith("transparent", "rgb(1, 1, 1)"))).toBe("rgb(1, 1, 1)");
});

test("backdropColor returns '' when both are transparent, so the caller uses white", () => {
  expect(backdropColor(winWith("transparent", "transparent"))).toBe("");
  expect(backdropColor(winWith("rgba(0, 0, 0, 0)", "rgba(255, 255, 255, 0)"))).toBe("");
});

test("backdropColor swallows a cross-origin throw", () => {
  const win = {
    get document(): never {
      throw new Error("cross-origin");
    },
  } as unknown as Window;
  expect(backdropColor(win)).toBe("");
});

// -- the pipeline order, which silently corrupts a picture when changed ------

const SRC = readFileSync(join(import.meta.dir, "domShot.ts"), "utf8");

test("images are inlined BEFORE canvases are rasterised", () => {
  const body = SRC.slice(SRC.indexOf("export async function domShot("));
  const images = body.indexOf("await inlineImages(");
  const raster = body.indexOf("rasteriseCanvases(");
  expect(images).toBeGreaterThan(-1);
  // rasteriseCanvases puts data:-URL <img>s of its OWN into the clone; running
  // it first would have inlineImages pair those against the source's real
  // images by index and put a chart's pixels where a logo was.
  expect(images).toBeLessThan(raster);
});

test("scroll compensation runs LAST, after the canvas swap", () => {
  const body = SRC.slice(SRC.indexOf("export async function domShot("));
  const raster = body.indexOf("rasteriseCanvases(");
  const scroll = body.indexOf("applyScrollOffsets(styles.scrolled)");
  // So a canvas swapped for an <img> is shifted with the rest of its scroll box
  // rather than left at the top of it.
  expect(raster).toBeLessThan(scroll);
});

test("the style walk stops short of the deadline so image fetches have a tail", () => {
  const body = SRC.slice(SRC.indexOf("export async function domShot("));
  // Sharing one stamp would let a big page spend the entire budget and leave
  // every picture inside it a placeholder in a capture that had time in hand.
  expect(body).toContain("deadline - IMG_TAIL_MS");
});

test("domShot refuses an empty body rather than capturing a blank", () => {
  const body = SRC.slice(SRC.indexOf("export async function domShot("));
  expect(body).toContain("isEmptyBody(body)");
  expect(body).toContain("return null");
});
