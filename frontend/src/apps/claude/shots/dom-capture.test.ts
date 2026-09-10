// THE DOM-CLONE CAPTURE's own machinery (T:9044-10022). `capture.test.ts` injects
// its strategies, so nothing there ever runs the style walk, the childList guard,
// the yield/deadline arithmetic, the image inlining or the backdrop read. This
// suite drives those functions directly against a hand-rolled DOM: what is under
// test is every DECISION each one makes — which verdict a walk returns, whose
// subtree is dropped when the source re-renders, what a scrolled box gets written
// onto it, which url() is worth fetching, and what a picture that could not be
// fetched leaves behind. No pixels: the fake elements only carry the members the
// production code actually reads.
import { afterAll, beforeEach, describe, expect, test } from "bun:test";
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();

// `bun test` runs every file in ONE process against one `globalThis`, so every
// global this suite stubs is snapshotted here and put back in `afterAll` — a
// leaked `fetch` or a leaked `Date.now` silently rewrites whoever runs next
// (the idiom capture.test.ts and platform/ui/appdoctor-lib.test.ts set out).
const G = globalThis as Record<string, unknown>;
const BEFORE = {
  fetch: G.fetch,
  FileReader: G.FileReader,
  now: Date.now,
  Image: G.Image,
  createImageBitmap: G.createImageBitmap,
  createObjectURL: URL.createObjectURL,
  revokeObjectURL: URL.revokeObjectURL,
  // `captureDom` reaches for these three as well: the serializer, and the
  // TOP-LEVEL `document`, which is where its output canvas comes from (the app's
  // own document is the source, never the destination).
  XMLSerializer: G.XMLSerializer,
  docCreate: (G.document as { createElement?: unknown } | undefined)?.createElement,
};
afterAll(() => {
  G.fetch = BEFORE.fetch;
  G.FileReader = BEFORE.FileReader;
  Date.now = BEFORE.now;
  G.Image = BEFORE.Image;
  G.createImageBitmap = BEFORE.createImageBitmap;
  Object.assign(URL, {
    createObjectURL: BEFORE.createObjectURL,
    revokeObjectURL: BEFORE.revokeObjectURL,
  });
  G.XMLSerializer = BEFORE.XMLSerializer;
  const doc = G.document as Record<string, unknown> | undefined;
  if (doc) {
    if (BEFORE.docCreate === undefined) delete doc.createElement;
    else doc.createElement = BEFORE.docCreate;
  }
});

// `dataUrl` (encode.ts) reads the bytes through a FileReader, which bun's test
// runtime does not have — and without it every successful fetch would look like a
// failed one, which is exactly the distinction these tests are checking. The stub
// does what the platform one does: base64 of the blob, announced on `onload`.
G.FileReader = class {
  result: string | null = null;
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  readAsDataURL(blob: Blob): void {
    void blob.arrayBuffer().then((buf) => {
      const bytes = new Uint8Array(buf);
      let raw = "";
      for (const b of bytes) raw += String.fromCharCode(b);
      this.result = "data:" + (blob.type || "application/octet-stream") + ";base64," + btoa(raw);
      this.onload?.();
    });
  }
};

const {
  applyScroll,
  backdrop,
  blankRegions,
  captureDom,
  caveatsOf,
  imageNote,
  imagePlaceholder,
  inlineImages,
  inlineStyles,
  paneNote,
  styleUrls,
  trustLine,
  urlAsData,
  viewNoteFrom,
} = await import("./dom-capture");
const { SHOT_IMG_MAX, SHOT_MAX_ELEMENTS, SHOT_STYLE_CHUNK } = await import("./types");

// ── the fake DOM ────────────────────────────────────────────────────────────
//
// Only what dom-capture.ts touches: attributes (which is all `cloneNode` would
// have carried), a live `children` array, the scroll PROPERTIES that have no
// markup, `isConnected` (the detached case the style walk exists to notice), and
// the four selectors `inlineImages` asks for.

interface FakeStyle {
  length: number;
  getPropertyValue(prop: string): string;
  [i: number]: string;
}

class El {
  attrs = new Map<string, string>();
  kids: El[] = [];
  parent: El | null = null;
  isConnected = true;
  scrollTop = 0;
  scrollLeft = 0;
  textContent = "";
  /** `captureDom`'s canvas size: `<html>`'s client box, `<body>`'s offset box as
   *  the fallback. */
  clientWidth = 0;
  clientHeight = 0;
  offsetWidth = 0;
  offsetHeight = 0;
  /** `<img>` only, and `currentSrc` before `src` on purpose: it is what the
   *  browser PICKED out of a srcset, which is the picture on screen. */
  currentSrc = "";
  alt = "";
  naturalWidth = 0;
  naturalHeight = 0;
  /** What a canvas hands back, so the `urlAsData` second chance can be driven
   *  down both roads: a data URL, or the throw of a tainted canvas. */
  dataUrl: string | (() => string) = "data:image/png;base64,CANVAS";
  drawn = 0;
  width = 0;
  height = 0;

  constructor(
    public tag: string,
    public ownerDocument: Doc,
  ) {}

  get children(): El[] {
    return this.kids;
  }

  append(...kids: El[]): El {
    for (const k of kids) {
      k.parent = this;
      this.kids.push(k);
    }
    return this;
  }

  setAttribute(name: string, value: string): void {
    this.attrs.set(name, value);
  }

  getAttribute(name: string): string | null {
    const v = this.attrs.get(name);
    return v === undefined ? null : v;
  }

  removeAttribute(name: string): void {
    this.attrs.delete(name);
  }

  remove(): void {
    if (!this.parent) return;
    this.parent.kids = this.parent.kids.filter((k) => k !== this);
    this.parent = null;
  }

  replaceWith(other: El): void {
    const p = this.parent;
    if (!p) return;
    p.kids = p.kids.map((k) => (k === this ? other : k));
    other.parent = p;
    this.parent = null;
  }

  getContext(kind: string): { drawImage: () => void } | null {
    return kind === "2d" ? { drawImage: () => void this.drawn++ } : null;
  }

  toDataURL(): string {
    return typeof this.dataUrl === "function" ? this.dataUrl() : this.dataUrl;
  }

  /** `body.cloneNode(true)` — attributes and children, a separate object, which
   *  is the pairing every function in the walk depends on. */
  cloneNode(deep: boolean): El {
    const c = new El(this.tag, this.ownerDocument);
    for (const [k, v] of this.attrs) c.attrs.set(k, v);
    if (deep) c.append(...this.kids.map((k) => k.cloneNode(true)));
    return c;
  }

  descendants(): El[] {
    const out: El[] = [];
    const walk = (e: El): void => {
      for (const k of e.kids) {
        out.push(k);
        walk(k);
      }
    };
    walk(this);
    return out;
  }

  querySelectorAll(sel: string): El[] {
    const all = this.descendants();
    if (sel === "*") return all;
    if (sel === "picture source") {
      return all.filter((e) => e.tag === "source" && e.parent?.tag === "picture");
    }
    return all.filter((e) => e.tag === sel);
  }
}

class Doc {
  documentElement = new El("html", this);
  body = new El("body", this);
  defaultView: Win | null = null;
  /** The clone's document is where `imagePlaceholder`'s box and `urlAsData`'s
   *  fallback canvas come from, so a test that needs one of those to misbehave
   *  reaches it here. */
  onCreate: ((el: El) => void) | null = null;
  createElement(tag: string): El {
    const made = new El(tag, this);
    this.onCreate?.(made);
    return made;
  }
}

class Win {
  document = new Doc();
  MutationObserver: unknown = null;
  /** Every property name/value pair a walk will copy, plus a hook a test can use
   *  to make the source re-render mid-walk — a computed-style read is the only
   *  place the walk yields control to us synchronously. */
  onStyle: ((el: El) => void) | null = null;
  props: [string, string][] = [
    ["color", "rgb(0, 0, 0)"],
    ["display", "block"],
  ];
  bg = new Map<El, string>();
  styleReads: El[] = [];
  constructor() {
    this.document.defaultView = this;
  }
  getComputedStyle(el: El): FakeStyle {
    this.styleReads.push(el);
    this.onStyle?.(el);
    const pairs = this.props;
    const cs: FakeStyle = {
      length: pairs.length,
      getPropertyValue: (p: string) => pairs.find(([k]) => k === p)?.[1] || "",
    };
    pairs.forEach(([k], i) => {
      cs[i] = k;
    });
    // `backdrop` reads one member that is not a longhand enumeration.
    (cs as unknown as { backgroundColor: string }).backgroundColor = this.bg.get(el) || "";
    return cs;
  }
}

/** A source tree and its clone, paired the way `body.cloneNode(true)` pairs them
 *  — same shape, separate objects, so a style written to one is observable as
 *  NOT having gone to the other. */
function tree(shape: (make: (tag?: string) => El) => El, win: Win): { src: El; dst: El } {
  const doc = win.document;
  const make = (tag = "div"): El => new El(tag, doc);
  const src = shape(make);
  const copy = (e: El): El => {
    const c = new El(e.tag, doc);
    for (const [k, v] of e.attrs) c.attrs.set(k, v);
    c.append(...e.kids.map(copy));
    return c;
  };
  return { src, dst: copy(src) };
}

/** `n` children under one root: the shape that makes the walk's caps and its
 *  every-SHOT_STYLE_CHUNK yield reachable without a deep tree. */
function wide(n: number, win: Win): { src: El; dst: El } {
  return tree((make) => {
    const root = make("body");
    for (let i = 0; i < n; i++) root.append(make());
    return root;
  }, win);
}

const el = (e: El): Element => e as unknown as Element;
const asWin = (w: Win): Window => w as unknown as Window;

/** A MutationObserver whose deliveries a test decides. `records` is what the
 *  next `takeRecords()` drain hands over; `fire` is the callback road. */
function fakeMO(win: Win): {
  drains: number;
  observed: unknown[];
  disconnects: number;
  records: { type: string; target: El }[];
  fire: (recs: { type: string; target: El }[]) => void;
} {
  const state = {
    drains: 0,
    observed: [] as unknown[],
    disconnects: 0,
    records: [] as { type: string; target: El }[],
    fire: (_recs: { type: string; target: El }[]) => {},
  };
  win.MutationObserver = class {
    constructor(cb: (recs: unknown[]) => void) {
      state.fire = (recs) => cb(recs);
    }
    observe(target: unknown, opts: unknown): void {
      state.observed.push({ target, opts });
    }
    takeRecords(): unknown[] {
      state.drains++;
      const out = state.records;
      state.records = [];
      return out;
    }
    disconnect(): void {
      state.disconnects++;
    }
  };
  return state;
}

/** A monotonic clock the tests own, so "past the deadline" is arithmetic and not
 *  a race with the machine. Every computed-style read costs 1ms — which is the
 *  real cost model: the walk's whole expense is that one call per element. */
function clockPerStyle(win: Win, start = 1_000_000): void {
  let t = start;
  Date.now = () => t;
  const prev = win.onStyle;
  win.onStyle = (e) => {
    t++;
    prev?.(e);
  };
}

beforeEach(() => {
  Date.now = BEFORE.now;
  G.fetch = BEFORE.fetch;
});

describe("inlineStyles (T:9044-9218)", () => {
  test("a quiet tree is complete: every clone gets the computed longhands, and the source is untouched", async () => {
    const win = new Win();
    const { src, dst } = tree((make) => make("body").append(make().append(make())), win);
    const out = await inlineStyles(el(src), el(dst), Date.now() + 10_000);
    expect(out.incomplete).toBe("");
    // Three elements: the root and both descendants — breadth-first, so the root
    // is styled before anything under it.
    expect(out.styled).toBe(3);
    expect(dst.getAttribute("style")).toBe("color:rgb(0, 0, 0);display:block;");
    expect(dst.kids[0].kids[0].getAttribute("style")).toBe("color:rgb(0, 0, 0);display:block;");
    // The walk writes to the CLONE only; a write to the live tree would mutate
    // the page the user is looking at.
    expect(src.getAttribute("style")).toBeNull();
  });

  test("the observer watches the SOURCE, childList only, and is disconnected on the way out (T:9146)", async () => {
    const win = new Win();
    const mo = fakeMO(win);
    const { src, dst } = tree((make) => make("body").append(make()), win);
    await inlineStyles(el(src), el(dst), Date.now() + 10_000);
    expect(mo.observed).toEqual([{ target: src, opts: { subtree: true, childList: true } }]);
    // Observing attributes would put the distrust note on every capture of a page
    // with a clock in it, so `attributes` is deliberately absent above.
    expect(mo.disconnects).toBe(1);
  });

  test("a childList record DROPS that parent's subtree rather than pairing it wrong (T:9163)", async () => {
    const win = new Win();
    const mo = fakeMO(win);
    const { src, dst } = tree(
      (make) => make("body").append(make().append(make(), make())),
      win,
    );
    const child = src.kids[0];
    // The re-render lands while the walk is inside the root — a computed-style
    // read is the one synchronous handoff there is. By the time `child` is
    // dequeued, `reshaped` already holds it.
    win.onStyle = (e) => {
      if (e === src) mo.fire([{ type: "childList", target: child }]);
    };
    const out = await inlineStyles(el(src), el(dst), Date.now() + 10_000);
    expect(out.incomplete).toBe("mutated");
    // Root and child are styled; the child's two grandchildren are NOT — pairing
    // them would have dressed one element in another's layout.
    expect(out.styled).toBe(2);
    expect(dst.kids[0].kids[0].getAttribute("style")).toBeNull();
  });

  test("a non-childList record still says 'mutated' but keeps the pairing", async () => {
    const win = new Win();
    const mo = fakeMO(win);
    const { src, dst } = tree((make) => make("body").append(make().append(make())), win);
    win.onStyle = (e) => {
      if (e === src) mo.fire([{ type: "attributes", target: src.kids[0] }]);
    };
    const out = await inlineStyles(el(src), el(dst), Date.now() + 10_000);
    // Only a changed CHILD LIST breaks index pairing, so the descent continues
    // and all three elements are styled — the verdict is about the picture being
    // a blend, not about the styles landing on strangers.
    expect(out.incomplete).toBe("mutated");
    expect(out.styled).toBe(3);
  });

  test("a child-count mismatch is caught without any observer at all", async () => {
    const win = new Win();
    // No MutationObserver on the view: the compare is the fallback detector, and
    // losing the observer is a loss of precision, not of correctness.
    const { src, dst } = tree((make) => make("body").append(make(), make()), win);
    dst.kids.pop();
    const out = await inlineStyles(el(src), el(dst), Date.now() + 10_000);
    expect(out.incomplete).toBe("mutated");
    expect(out.styled).toBe(1);
  });

  test("an observer whose observe() throws is dropped, and the walk still completes", async () => {
    const win = new Win();
    win.MutationObserver = class {
      observe(): void {
        throw new Error("frame gone");
      }
      takeRecords(): unknown[] {
        throw new Error("frame gone");
      }
      disconnect(): void {}
    };
    const { src, dst } = tree((make) => make("body").append(make()), win);
    const out = await inlineStyles(el(src), el(dst), Date.now() + 10_000);
    expect(out).toMatchObject({ styled: 2, incomplete: "" });
  });

  test("a disconnected node is 'detached', and its subtree is skipped", async () => {
    const win = new Win();
    const { src, dst } = tree((make) => make("body").append(make().append(make())), win);
    src.kids[0].isConnected = false;
    const out = await inlineStyles(el(src), el(dst), Date.now() + 10_000);
    expect(out.incomplete).toBe("detached");
    // The root only: a detached element has no cascade, so it is skipped rather
    // than styled from an empty enumeration, and nothing under it is queued.
    expect(out.styled).toBe(1);
  });

  test("mutated OUTRANKS detached: correctness news beats budget news (T:9110)", async () => {
    const win = new Win();
    const { src, dst } = tree((make) => make("body").append(make(), make()), win);
    src.kids[0].isConnected = false;
    dst.kids.pop();
    const out = await inlineStyles(el(src), el(dst), Date.now() + 10_000);
    expect(out.incomplete).toBe("mutated");
  });

  test("the element cap stops the walk at SHOT_MAX_ELEMENTS and says so", async () => {
    const win = new Win();
    const { src, dst } = wide(SHOT_MAX_ELEMENTS, win);
    const out = await inlineStyles(el(src), el(dst), Date.now() + 60_000);
    expect(out.styled).toBe(SHOT_MAX_ELEMENTS);
    expect(out.incomplete).toBe("elements");
    // The cap is a break, not a skip: the tail of the queue is simply never read,
    // so the last children keep no style at all.
    expect(dst.kids[SHOT_MAX_ELEMENTS - 1].getAttribute("style")).toBeNull();
  });

  test("the deadline is re-read per element, so the stop lands where the clock says", async () => {
    const win = new Win();
    const { src, dst } = wide(50, win);
    clockPerStyle(win, 1_000_000);
    // Six elements' worth of budget: the check runs BEFORE each style read, so
    // the 7th iteration is the one that finds the clock past the stamp.
    const out = await inlineStyles(el(src), el(dst), 1_000_005);
    expect(out.styled).toBe(6);
    expect(out.incomplete).toBe("deadline");
  });

  test("an already-expired deadline styles nothing rather than throwing", async () => {
    const win = new Win();
    const { src, dst } = wide(3, win);
    const out = await inlineStyles(el(src), el(dst), Date.now() - 1);
    expect(out).toMatchObject({ styled: 0, incomplete: "deadline" });
    expect(win.styleReads.length).toBe(0);
  });

  test("it yields every SHOT_STYLE_CHUNK elements, and drains the observer each time (T:9163)", async () => {
    const win = new Win();
    const mo = fakeMO(win);
    const { src, dst } = wide(2 * SHOT_STYLE_CHUNK, win);
    const out = await inlineStyles(el(src), el(dst), Date.now() + 60_000);
    expect(out.styled).toBe(2 * SHOT_STYLE_CHUNK + 1);
    // 401 elements is two whole chunks, so two yields — each followed by a drain
    // — plus the one final drain in the `finally`, which is the only place a
    // mutation delivered during the last chunk can still be seen.
    expect(mo.drains).toBe(3);
    expect(out.incomplete).toBe("");
  });

  test("a mutation delivered during the LAST chunk is still reported, by the final drain", async () => {
    const win = new Win();
    const mo = fakeMO(win);
    const { src, dst } = tree((make) => make("body").append(make()), win);
    // Queued for delivery, never handed to the callback: without the drain in the
    // `finally` this capture would ship as trustworthy.
    mo.records = [{ type: "childList", target: src }];
    const out = await inlineStyles(el(src), el(dst), Date.now() + 10_000);
    expect(out.incomplete).toBe("mutated");
  });

  test("scrolled boxes are recorded against the CLONE, and the root's own scroll is not (T:9186)", async () => {
    const win = new Win();
    const { src, dst } = tree((make) => make("body").append(make(), make()), win);
    // The root is the app's <body>: its offset is the WINDOW's, and the caller
    // already shifts the whole clone by that — recording it here would scroll the
    // capture twice.
    src.scrollTop = 900;
    src.kids[0].scrollTop = 40;
    src.kids[0].scrollLeft = 7;
    const out = await inlineStyles(el(src), el(dst), Date.now() + 10_000);
    expect(out.scrolled).toEqual([{ clone: el(dst.kids[0]), x: 7, y: 40 }]);
  });
});

describe("applyScroll (T:9219-9265)", () => {
  const win = new Win();

  test("each child of a scrolled box is translated back, and the count is the children moved", () => {
    const { dst } = tree((make) => make("div").append(make(), make()), win);
    const shifted = applyScroll([{ clone: el(dst), x: 12, y: 300 }]);
    expect(shifted).toBe(2);
    // A transform on the CHILDREN, not an offset on the parent: it is the paint
    // the browser does when it scrolls, and it changes no layout.
    for (const k of dst.kids) {
      expect(k.getAttribute("style")).toBe(";transform:translate(-12px,-300px);");
    }
  });

  test("fixed and sticky children do not move with the scroll", () => {
    const { dst } = tree((make) => make("div").append(make(), make(), make()), win);
    dst.kids[0].setAttribute("style", "position:fixed;top:0");
    dst.kids[1].setAttribute("style", "color:red;position:sticky;");
    dst.kids[2].setAttribute("style", "position:absolute;");
    const shifted = applyScroll([{ clone: el(dst), x: 0, y: 100 }]);
    // A stuck header stays where the user saw it; `absolute` is not stuck and is
    // shifted like everything else.
    expect(shifted).toBe(1);
    expect(dst.kids[0].getAttribute("style")).toBe("position:fixed;top:0");
    expect(dst.kids[1].getAttribute("style")).toBe("color:red;position:sticky;");
    expect(dst.kids[2].getAttribute("style")).toContain("transform:translate(0px,-100px)");
  });

  test("the child's own transform is COMPOSED with, ours leftmost, and 'none' is dropped", () => {
    const { dst } = tree((make) => make("div").append(make(), make()), win);
    dst.kids[0].setAttribute("style", "transform: scale(2) ;");
    dst.kids[1].setAttribute("style", "transform:none;");
    applyScroll([{ clone: el(dst), x: 5, y: 5 }]);
    // Leftmost is outermost, so the scroll shift has to precede the element's own
    // transform or the two multiply in the wrong order.
    expect(dst.kids[0].getAttribute("style")).toBe(
      "transform: scale(2) ;;transform:translate(-5px,-5px) scale(2);",
    );
    expect(dst.kids[1].getAttribute("style")).toBe(
      "transform:none;;transform:translate(-5px,-5px);",
    );
  });

  test("nothing to shift is 0, including a missing list", () => {
    expect(applyScroll(null)).toBe(0);
    expect(applyScroll(undefined)).toBe(0);
    expect(applyScroll([])).toBe(0);
    // A scrolled box with no children has nothing to translate.
    expect(applyScroll([{ clone: el(new El("div", new Doc())), x: 1, y: 1 }])).toBe(0);
  });
});

describe("styleUrls (T:9451)", () => {
  test("quoted and unquoted url() references alike, in order", () => {
    expect(
      styleUrls("background-image:url('a.png'),url(\"b.png\"),url(c.png),url( d.png );"),
    ).toEqual(["a.png", "b.png", "c.png", "d.png"]);
  });

  test("the two already-local forms are skipped: data: and an SVG fragment", () => {
    // Nothing to fetch for `data:` and nothing to fetch for `url(#id)` — the
    // fragment target travels with the clone.
    expect(styleUrls("background:url(data:image/png;base64,AAA);mask:url(#clip);")).toEqual([]);
    expect(styleUrls("mask:url('#clip');border-image:url(edge.svg)")).toEqual(["edge.svg"]);
  });

  test("a style with no url() at all, and one whose url() is empty", () => {
    expect(styleUrls("color:red;display:block")).toEqual([]);
    expect(styleUrls("")).toEqual([]);
    expect(styleUrls("background:url();")).toEqual([]);
  });
});

describe("imagePlaceholder (T:9466)", () => {
  test("a dashed box the size of the image, carrying the alt text, in the image's place", () => {
    const doc = new Doc();
    const parent = new El("div", doc);
    const img = new El("img", doc);
    img.setAttribute("style", "width:120px;height:40px;");
    parent.append(img);
    imagePlaceholder(el(img), "the sales chart");
    const box = parent.kids[0];
    // Replaced, not hidden: nothing at all would silently redraw the layout
    // around a hole the user's screen did not have.
    expect(box).not.toBe(img);
    expect(box.tag).toBe("div");
    expect(box.textContent).toBe("image not captured — the sales chart");
    // The image's own computed style comes FIRST, so the box keeps its box.
    const style = box.getAttribute("style") || "";
    expect(style.startsWith("width:120px;height:40px;;")).toBe(true);
    expect(style).toContain("border:1px dashed #b4b4b4");
  });

  test("a missing or empty alt is the bare sentence, never a broken-image glyph", () => {
    const doc = new Doc();
    for (const alt of [null, undefined, ""]) {
      const parent = new El("div", doc);
      const img = new El("img", doc);
      parent.append(img);
      imagePlaceholder(el(img), alt);
      // A broken-image glyph would read as a bug in the page being photographed.
      expect(parent.kids[0].textContent).toBe("image not captured");
      expect(img.parent).toBeNull();
    }
  });
});

// ── the image roads (T:9430-9552) ───────────────────────────────────────────

function okFetch(seen: string[]): void {
  G.fetch = (url: string) => {
    seen.push(String(url));
    return Promise.resolve({
      ok: true,
      status: 200,
      blob: () => Promise.resolve(new Blob([new Uint8Array([1, 2, 3])], { type: "image/png" })),
    });
  };
}

describe("urlAsData (T:9430)", () => {
  test("a fetchable URL comes back as data: bytes", async () => {
    const seen: string[] = [];
    okFetch(seen);
    const out = await urlAsData("/logo.png", null);
    expect(seen).toEqual(["/logo.png"]);
    expect(out.startsWith("data:image/png;base64,")).toBe(true);
  });

  test("a non-ok response is a failure, not empty bytes", async () => {
    G.fetch = () => Promise.resolve({ ok: false, status: 404, blob: () => Promise.resolve(null) });
    // Nothing to fall back to with no element, so the HTTP status is what the
    // caller gets — a 404 that returned "" would embed a blank picture and call
    // it captured.
    await expect(urlAsData("/gone.png", null)).rejects.toThrow("HTTP 404");
  });

  test("a loaded element is the SECOND chance: the canvas gets the pixels back", async () => {
    G.fetch = () => Promise.reject(new Error("CORS"));
    const doc = new Doc();
    const img = new El("img", doc);
    img.naturalWidth = 64;
    img.naturalHeight = 32;
    const out = await urlAsData("https://other.example/x.png", img as unknown as HTMLImageElement);
    // A cross-origin image without CORS headers cannot be fetched, but the
    // browser already decoded it into the element.
    expect(out).toBe("data:image/png;base64,CANVAS");
  });

  test("an unloaded element rethrows the fetch failure rather than drawing nothing", async () => {
    G.fetch = () => Promise.reject(new Error("offline"));
    const img = new El("img", new Doc());
    img.naturalWidth = 0;
    await expect(
      urlAsData("/x.png", img as unknown as HTMLImageElement),
    ).rejects.toThrow("offline");
    await expect(urlAsData("/x.png", null)).rejects.toThrow("offline");
  });

  test("a tainted canvas throws out of toDataURL, and that throw is the answer", async () => {
    G.fetch = () => Promise.reject(new Error("CORS"));
    const doc = new Doc();
    const img = new El("img", doc);
    img.naturalWidth = 10;
    img.naturalHeight = 10;
    doc.onCreate = (made) => {
      made.dataUrl = () => {
        throw new Error("SecurityError: tainted canvases may not be exported");
      };
    };
    // `toDataURL` throwing is precisely how a taint announces itself, so the
    // caller counts the image as missing instead of embedding a lie.
    await expect(urlAsData("/x.png", img as unknown as HTMLImageElement)).rejects.toThrow(
      "tainted",
    );
  });
});

describe("inlineImages (T:9491-9552)", () => {
  const imgTree = (
    win: Win,
    urls: string[],
  ): { src: El; dst: El } => {
    const out = tree((make) => {
      const root = make("body");
      for (const _u of urls) root.append(make("img"));
      return root;
    }, win);
    urls.forEach((u, i) => {
      out.src.kids[i].currentSrc = u;
      out.src.kids[i].alt = "alt" + i;
    });
    return out;
  };

  test("every img is rewritten to data:, and srcset/sizes are removed with it", async () => {
    const seen: string[] = [];
    okFetch(seen);
    const win = new Win();
    const { src, dst } = imgTree(win, ["/a.png", "/b.png"]);
    for (const k of dst.kids) {
      k.setAttribute("srcset", "/a@2x.png 2x");
      k.setAttribute("sizes", "100vw");
    }
    const out = await inlineImages(el(src), el(dst), Date.now() + 10_000);
    expect(out.missing).toBe(0);
    expect(seen).toEqual(["/a.png", "/b.png"]);
    for (const k of dst.kids) {
      expect((k.getAttribute("src") || "").startsWith("data:")).toBe(true);
      // Either attribute left in place would have the browser re-select a URL
      // over the src we just wrote.
      expect(k.getAttribute("srcset")).toBeNull();
      expect(k.getAttribute("sizes")).toBeNull();
    }
  });

  test("a repeated URL costs ONE fetch, and an inline data: src costs none", async () => {
    const seen: string[] = [];
    okFetch(seen);
    const win = new Win();
    const { src, dst } = imgTree(win, ["/same.png", "/same.png", "data:image/gif;base64,AA", ""]);
    const out = await inlineImages(el(src), el(dst), Date.now() + 10_000);
    expect(seen).toEqual(["/same.png"]);
    expect(out.missing).toBe(0);
    // Already local and already nothing: neither is touched, so neither is
    // counted against the picture.
    expect(dst.kids[2].getAttribute("src")).toBeNull();
  });

  test("currentSrc beats the src attribute: the picture on SCREEN is the one photographed", async () => {
    const seen: string[] = [];
    okFetch(seen);
    const win = new Win();
    const { src, dst } = imgTree(win, [""]);
    src.kids[0].setAttribute("src", "/small.png");
    src.kids[0].currentSrc = "/picked-from-srcset.png";
    await inlineImages(el(src), el(dst), Date.now() + 10_000);
    expect(seen).toEqual(["/picked-from-srcset.png"]);
  });

  test("a fetch that fails leaves the dashed placeholder and counts one missing", async () => {
    G.fetch = () => Promise.reject(new Error("offline"));
    const win = new Win();
    const { src, dst } = imgTree(win, ["/a.png", "/b.png"]);
    const out = await inlineImages(el(src), el(dst), Date.now() + 10_000);
    expect(out.missing).toBe(2);
    expect(dst.kids[0].textContent).toBe("image not captured — alt0");
    expect(dst.kids[1].textContent).toBe("image not captured — alt1");
  });

  test("past the deadline nothing new is fetched: the rest are placeholders", async () => {
    const seen: string[] = [];
    okFetch(seen);
    const win = new Win();
    const { src, dst } = imgTree(win, ["/a.png"]);
    const out = await inlineImages(el(src), el(dst), Date.now() - 1);
    expect(seen).toEqual([]);
    expect(out.missing).toBe(1);
    expect(dst.kids[0].textContent).toBe("image not captured — alt0");
  });

  test("SHOT_IMG_MAX bounds the DISTINCT urls, and the overflow is counted", async () => {
    const seen: string[] = [];
    okFetch(seen);
    const win = new Win();
    const urls = Array.from({ length: SHOT_IMG_MAX + 3 }, (_v, i) => "/i" + i + ".png");
    const { src, dst } = imgTree(win, urls);
    const out = await inlineImages(el(src), el(dst), Date.now() + 10_000);
    expect(seen.length).toBe(SHOT_IMG_MAX);
    expect(out.missing).toBe(3);
  });

  test("<source> elements are removed before any src is rewritten", async () => {
    const seen: string[] = [];
    okFetch(seen);
    const win = new Win();
    const { src, dst } = tree((make) => {
      const root = make("body");
      const pic = make("picture");
      pic.append(make("source"), make("source"), make("img"));
      return root.append(pic);
    }, win);
    src.kids[0].kids[2].currentSrc = "/hero.png";
    const out = await inlineImages(el(src), el(dst), Date.now() + 10_000);
    // A <source> still pointing at an http URL would have the browser re-resolve
    // the child over the src we just wrote.
    expect(dst.querySelectorAll("picture source")).toEqual([]);
    expect(out.missing).toBe(0);
    expect((dst.kids[0].kids[0].getAttribute("src") || "").startsWith("data:")).toBe(true);
  });

  test("background url()s are read back off the CLONE's style and rewritten in place", async () => {
    const seen: string[] = [];
    okFetch(seen);
    const win = new Win();
    const { src, dst } = tree((make) => make("body").append(make(), make()), win);
    dst.kids[0].setAttribute("style", "background-image:url(/tile.png);color:red");
    dst.kids[1].setAttribute("style", "color:blue");
    const out = await inlineImages(el(src), el(dst), Date.now() + 10_000);
    expect(out.missing).toBe(0);
    // The style walk already wrote the computed value onto the clone, so this
    // needs no second pass over the live tree.
    expect(seen).toEqual(["/tile.png"]);
    expect(dst.kids[0].getAttribute("style")).toBe(
      "background-image:url(data:image/png;base64,AQID);color:red",
    );
    // Untouched styles are not rewritten at all.
    expect(dst.kids[1].getAttribute("style")).toBe("color:blue");
  });

  test("a background that could not be fetched is counted but gets NO placeholder", async () => {
    G.fetch = () => Promise.reject(new Error("offline"));
    const win = new Win();
    const { src, dst } = tree((make) => make("body").append(make()), win);
    dst.kids[0].setAttribute("style", "background-image:url(/tile.png);color:red");
    const out = await inlineImages(el(src), el(dst), Date.now() + 10_000);
    expect(out.missing).toBe(1);
    // The element keeps its size and its colour: substituting a dashed box for a
    // texture would be a bigger lie than leaving it plain.
    expect(dst.kids[0].getAttribute("style")).toBe("background-image:url(/tile.png);color:red");
    expect(dst.kids[0].tag).toBe("div");
  });

  test("a background past the deadline is counted without being fetched", async () => {
    const seen: string[] = [];
    okFetch(seen);
    const win = new Win();
    const { src, dst } = tree((make) => make("body").append(make()), win);
    dst.kids[0].setAttribute("style", "background:url(/a.png),url(/b.png)");
    const out = await inlineImages(el(src), el(dst), Date.now() - 1);
    expect(seen).toEqual([]);
    expect(out.missing).toBe(2);
  });
});

describe("backdrop (T:10022)", () => {
  test("<html>'s background wins when it is opaque", () => {
    const win = new Win();
    win.bg.set(win.document.documentElement, "rgb(24, 24, 27)");
    win.bg.set(win.document.body, "rgb(255, 255, 255)");
    expect(backdrop(asWin(win))).toBe("rgb(24, 24, 27)");
  });

  test("<body> is the fallback when <html> is transparent, in either spelling", () => {
    const win = new Win();
    win.bg.set(win.document.documentElement, "transparent");
    win.bg.set(win.document.body, "rgb(250, 250, 250)");
    expect(backdrop(asWin(win))).toBe("rgb(250, 250, 250)");
    // A zero-alpha rgba paints nothing, so it is not a backdrop either.
    win.bg.set(win.document.documentElement, "rgba(0, 0, 0, 0)");
    expect(backdrop(asWin(win))).toBe("rgb(250, 250, 250)");
  });

  test("both transparent is \"\", which the caller reads as 'use white'", () => {
    const win = new Win();
    win.bg.set(win.document.documentElement, "rgba(255, 255, 255, 0)");
    win.bg.set(win.document.body, "transparent");
    expect(backdrop(asWin(win))).toBe("");
    // A partly transparent colour DOES paint, so it is kept.
    win.bg.set(win.document.body, "rgba(0, 0, 0, 0.5)");
    expect(backdrop(asWin(win))).toBe("rgba(0, 0, 0, 0.5)");
  });

  test("a frame that went away mid-read is \"\", never a throw", () => {
    const win = new Win();
    win.getComputedStyle = () => {
      throw new Error("cross-origin");
    };
    expect(backdrop(asWin(win))).toBe("");
    // A missing <body> takes the same road as a missing colour.
    const bare = new Win();
    bare.document.body = null as unknown as El;
    bare.bg.set(bare.document.documentElement, "transparent");
    expect(backdrop(asWin(bare))).toBe("");
  });
});

// ── the caveats (T:9275-9345, 10052) ────────────────────────────────────────
//
// These are the SENTENCES, and they are the fidelity thesis of the whole
// feature: a picture the model cannot fully trust has to say so in words the
// model will act on, and every cause the walk can report is worded in exactly
// one place. So they are pinned verbatim — a reworded caveat is a behaviour
// change, not a copy edit — along with the two decisions around them: which
// order they are joined in, and which trust line closes them.

type Pane = import("./types").PaneBitmap;

/** Only the members the builders read; the pixels are nobody's business here. */
function pane(over: Partial<Pane>): Pane {
  return {
    canvas: null as unknown as Pane["canvas"],
    width: 400,
    height: 300,
    blanks: [],
    styled: 0,
    incomplete: "",
    imagesMissing: 0,
    ...over,
  } as Pane;
}

describe("paneNote — one sentence per cause (T:9275)", () => {
  test("the element budget names the count it stopped at", () => {
    expect(paneNote(pane({ incomplete: "elements", styled: 3000 }))).toBe(
      "part of this capture is unstyled: the page has more elements than the capture budget allows (it stopped after 3000), so some of what you see may render without its CSS rather than as the user saw it",
    );
  });

  test("the deadline says it is about SPEED, not size — the reader's next move", () => {
    expect(paneNote(pane({ incomplete: "deadline", styled: 12 }))).toBe(
      "part of this capture is unstyled: it ran out of time after 12 elements, so some of what you see may render without its CSS rather than as the user saw it. This is about capture speed, not page size",
    );
  });

  test("detached is the page removing elements; mutated is the page RE-RENDERING", () => {
    // Two different pieces of news: one subtree lost its cascade, versus the
    // whole picture possibly being a blend of before and after.
    expect(paneNote(pane({ incomplete: "detached" }))).toBe(
      "part of this capture is unstyled: the page removed elements while the capture was running, so some of what you see may render without its CSS",
    );
    expect(paneNote(pane({ incomplete: "mutated" }))).toBe(
      "the page re-rendered while the capture was running, so this picture may not match what was on screen: some elements are missing their styling and the layout may be a blend of before and after",
    );
  });

  test("a complete capture says NOTHING, and neither does no capture at all", () => {
    expect(paneNote(pane({}))).toBe("");
    expect(paneNote(null)).toBe("");
  });
});

describe("imageNote — bounded doubt, and it counts (T:9304)", () => {
  test("one image is singular all the way through the sentence", () => {
    expect(imageNote(pane({ imagesMissing: 1 }))).toBe(
      '1 image could not be embedded in this picture and shows as a dashed "image not captured" box (or, for a background, as plain colour): a page rasterised through SVG cannot load a URL, so every image has to be fetched and inlined first, and that one could not be. The app is very likely showing the image fine',
    );
  });

  test("more than one is plural all the way through it", () => {
    expect(imageNote(pane({ imagesMissing: 4 }))).toBe(
      '4 images could not be embedded in this picture and show as a dashed "image not captured" box (or, for a background, as plain colour): a page rasterised through SVG cannot load a URL, so every image has to be fetched and inlined first, and those could not be. The app is very likely showing the image fine',
    );
  });

  test("none is silence", () => {
    expect(imageNote(pane({}))).toBe("");
    expect(imageNote(null)).toBe("");
  });
});

describe("trustLine — chosen by the WORST doubt present (T:9332)", () => {
  test("unbounded doubt replaces the reassurance with corroboration", () => {
    // Anything the style walk could not finish is doubt over an unknown set of
    // elements, so the closing instruction changes from "the rest is fine" to
    // "do not act on this alone".
    for (const bad of ["elements", "deadline", "detached", "mutated"] as const) {
      expect(trustLine(bad)).toBe(
        "Do not act on this image alone — check anything you read from it against the element's anchor and the DOM outline first.",
      );
    }
  });

  test("bounded doubt keeps it: missing pictures and blank canvases are VISIBLE", () => {
    expect(trustLine("")).toBe("The rest of the image is what the user saw.");
    expect(trustLine(false)).toBe("The rest of the image is what the user saw.");
    expect(trustLine(undefined)).toBe("The rest of the image is what the user saw.");
  });
});

describe("blankRegions — where the WebGL holes are, as prose (T:10052)", () => {
  const rect = (r: { left: number; top: number; width: number; height: number }) => () => r;

  test("one region, in pane-bitmap coordinates, grown to whole pixels", () => {
    const cv = new El("canvas", new Doc());
    const out = blankRegions(
      pane({ blanks: [el(cv)] }),
      rect({ left: 10.4, top: 20.6, width: 100.2, height: 50.9 }),
    );
    expect(out).toBe(
      "the following region is a WebGL canvas whose pixels could not be read back, so it shows the app's background instead of what was drawn there: 101x52 at (10,20). A map/3D library (maplibre, deck.gl) creates its context with preserveDrawingBuffer:false, which is why — the app is very likely drawing fine, so judge the layout around those regions and not inside them",
    );
  });

  test("two regions turn the sentence plural and are listed in order", () => {
    const doc = new Doc();
    const a = new El("canvas", doc);
    const b = new El("canvas", doc);
    const boxes = new Map<El, { left: number; top: number; width: number; height: number }>([
      [a, { left: 0, top: 0, width: 40, height: 40 }],
      [b, { left: 100, top: 10, width: 30, height: 30 }],
    ]);
    const out = blankRegions(pane({ blanks: [el(a), el(b)] }), (e) => boxes.get(e as unknown as El)!);
    expect(out).toContain("the following regions are a WebGL canvas");
    expect(out).toContain("they show the app's background");
    expect(out).toContain("40x40 at (0,0), 30x30 at (100,10)");
  });

  test("a canvas the crop refuses is not a region — and then there is no sentence", () => {
    // `cropRect` drops anything under SHOT_MIN_AREA or off the bitmap, and a
    // hairline slider is not news; the caveat must not announce an empty list.
    const cv = new El("canvas", new Doc());
    expect(
      blankRegions(pane({ blanks: [el(cv)] }), rect({ left: 0, top: 0, width: 4, height: 4 })),
    ).toBe("");
    expect(
      blankRegions(pane({ blanks: [el(cv)] }), rect({ left: 900, top: 0, width: 50, height: 50 })),
    ).toBe("");
    expect(blankRegions(pane({}), rect({ left: 0, top: 0, width: 9, height: 9 }))).toBe("");
    expect(blankRegions(null)).toBe("");
  });
});

describe("caveatsOf / viewNoteFrom — the order and the join (T:10111)", () => {
  const wide = () => () => ({ left: 0, top: 0, width: 40, height: 40 });

  test("blanks, then images, then the pane note", () => {
    // WIDEST-FIRST is the order: where the picture is not the app at all, then
    // what is missing from it, then what may be mis-styled in it.
    const cv = new El("canvas", new Doc());
    const out = caveatsOf(
      pane({ blanks: [el(cv)], imagesMissing: 2, incomplete: "deadline", styled: 7 }),
      wide(),
    );
    expect(out).toHaveLength(3);
    expect(out[0]!.startsWith("the following region is a WebGL canvas")).toBe(true);
    expect(out[1]!.startsWith("2 images could not be embedded")).toBe(true);
    expect(out[2]!.startsWith("part of this capture is unstyled: it ran out of time")).toBe(true);
  });

  test("only the causes that happened, and no capture is no caveats", () => {
    expect(caveatsOf(pane({ imagesMissing: 1 }))).toHaveLength(1);
    expect(caveatsOf(pane({}))).toEqual([]);
    expect(caveatsOf(null)).toEqual([]);
  });

  test('joined "; ", full stop, then the trust line — and "" when there is nothing to say', () => {
    expect(viewNoteFrom(["one", "two"], "")).toBe(
      "one; two. The rest of the image is what the user saw.",
    );
    expect(viewNoteFrom(["one"], "mutated")).toBe(
      "one. Do not act on this image alone — check anything you read from it against the element's anchor and the DOM outline first.",
    );
    // No caveats means no trust line either: a clean capture makes no claims.
    expect(viewNoteFrom([], "")).toBe("");
  });
});

// ── captureDom, end to end (T:9958's third path) ────────────────────────────

interface Canvas2D {
  fills: { colour: string; box: number[] }[];
  drawn: number;
}

/** The three globals `captureDom` reaches for beyond the app's own document: the
 *  serializer, the `<img>` that rasterises the SVG, and the TOP-LEVEL document
 *  the output canvas comes from. */
function installRaster(loads: boolean): { xml: string[]; ctx: Canvas2D; canvas: () => Record<string, unknown> } {
  const xml: string[] = [];
  const ctx: Canvas2D = { fills: [], drawn: 0 };
  G.XMLSerializer = class {
    serializeToString(node: unknown): string {
      xml.push((node as El).tag);
      return "<" + (node as El).tag + "/>";
    }
  };
  G.Image = class {
    onload: (() => void) | null = null;
    onerror: (() => void) | null = null;
    set src(_v: string) {
      // Asynchronous, like the real one: the production code installs both
      // handlers before assigning `src`.
      setTimeout(() => (loads ? this.onload?.() : this.onerror?.()), 0);
    }
  };
  let made: Record<string, unknown> = {};
  const out = {
    width: 0,
    height: 0,
    getContext: (kind: string) =>
      kind === "2d"
        ? {
            fillStyle: "",
            fillRect(x: number, y: number, w: number, h: number) {
              ctx.fills.push({ colour: String(this.fillStyle), box: [x, y, w, h] });
            },
            drawImage: () => void ctx.drawn++,
          }
        : null,
  };
  made = out as unknown as Record<string, unknown>;
  (G.document as Record<string, unknown>).createElement = (tag: string) =>
    tag === "canvas" ? made : {};
  return { xml, ctx, canvas: () => made };
}

/** A window whose body is a small styled tree, sized the way a real one is. */
function page(): Win {
  const win = new Win();
  const body = win.document.body;
  body.clientWidth = 0;
  body.offsetWidth = 640;
  body.offsetHeight = 480;
  win.document.documentElement.clientWidth = 800;
  win.document.documentElement.clientHeight = 600;
  body.append(new El("div", win.document), new El("span", win.document));
  win.bg.set(win.document.documentElement, "rgb(255, 255, 254)");
  return win;
}

describe("captureDom", () => {
  test("the happy path: the clone is styled, serialized and painted on a backdrop", async () => {
    const win = page();
    const { xml, ctx, canvas } = installRaster(true);
    const out = await captureDom(asWin(win), Date.now() + 10_000);
    expect(out).not.toBeNull();
    // `<html>`'s client box wins over `<body>`'s offset box.
    expect(out!.width).toBe(800);
    expect(out!.height).toBe(600);
    // The body and its two children were styled, and the count is what the
    // caveats report.
    expect(out!.styled).toBe(3);
    expect(out!.incomplete).toBe("");
    expect(out!.imagesMissing).toBe(0);
    expect(out!.blanks).toEqual([]);
    // The CLONE was serialized, never the live body: putting the clone into the
    // app's document would duplicate every id and mutate the page on screen.
    expect(xml).toEqual(["body"]);
    expect(win.document.body.getAttribute("style")).toBeNull();
    // A foreignObject paints nothing where the page is transparent and <html>'s
    // background does not travel with <body>'s clone, so the backdrop is filled
    // FIRST and the picture drawn over it (T:10009).
    expect(ctx.fills).toEqual([{ colour: "rgb(255, 255, 254)", box: [0, 0, 800, 600] }]);
    expect(ctx.drawn).toBe(1);
    expect(out!.canvas).toBe(canvas() as unknown as Pane["canvas"]);
    expect((canvas() as { width: number }).width).toBe(800);
  });

  test("a page with no <body> is `null`, not a throw", async () => {
    const win = new Win();
    win.document.body = null as unknown as El;
    expect(await captureDom(asWin(win), Date.now() + 10_000)).toBeNull();
    expect(await captureDom(null as unknown as Window, Date.now() + 10_000)).toBeNull();
  });

  test("markup the browser will not rasterise says what it MEANS (T:9999)", async () => {
    // An <img> load failure carries no reason at all, so reporting the event
    // would be reporting nothing: the one thing it can mean is that the clone
    // did not serialize as valid XHTML.
    const win = page();
    installRaster(false);
    await expect(captureDom(asWin(win), Date.now() + 10_000)).rejects.toThrow(
      "the pane's markup could not be rasterised",
    );
  });
});
