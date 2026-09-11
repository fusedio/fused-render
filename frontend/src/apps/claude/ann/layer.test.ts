// THE LAYER: the pins' CLASS, the render queue's disposal, and the layer host's
// removal.
//
// The class is not a detail. A pin is a node of the document the mode's
// dismissal listeners are attached to, so the popover's exemption list has to
// name the class `paintPins` actually writes — it named `.c-annpin` while the
// painter wrote `annpin`, which meant a mousedown on a pin dismissed the
// composer before the pin's own click could toggle it closed (A14). One
// assertion, on both sides of that contract at once.
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();

import { describe, expect, test } from "bun:test";

const { ANN_LAYER_CSS, createRenderQueue, createXOLayer, paintPins, removeLayer } = await import(
  "./layer"
);
const { ANN_BAR_TOKENS } = await import("./types");
const { ANN_DISMISS_EXEMPT, dismissesComposer } = await import("./AnnPopover");
import type { Annotation } from "./types";

/** A pins container with just the surface the painter touches. */
function pinsBox() {
  const kids: Array<Record<string, unknown>> = [];
  const box = {
    innerHTML: "",
    style: {} as Record<string, string>,
    appendChild(n: Record<string, unknown>) {
      kids.push(n);
    },
    ownerDocument: {
      createElement: () => ({
        className: "",
        style: {} as Record<string, string>,
        textContent: "",
        title: "",
        onclick: null as null | (() => void),
      }),
    },
  };
  return { box: box as unknown as Element, kids };
}

const pointNote = (over: Partial<Annotation> = {}): Annotation =>
  ({
    id: "n1",
    content: "make this blue",
    kind: "point",
    x: 40,
    y: 60,
    createdAt: 2000,
    ...over,
  }) as Annotation;

function paint(list: Annotation[]) {
  const p = pinsBox();
  paintPins({
    pins: p.box,
    stage: { clientWidth: 800, clientHeight: 600 },
    armed: true,
    list,
    roundStart: 1000,
    // A point note's coordinate is PAGE-space, so the paint needs a scroll to
    // convert it back through: the framed document's own window.
    doc: { defaultView: { scrollX: 0, scrollY: 0 } } as unknown as Document,
    xo: false,
    resolve: () => null,
    onPinClick: () => {},
  });
  return p;
}

describe("paintPins writes the class the composer's dismissal exempts", () => {
  test("a pin's class is `annpin`, and that is what ANN_DISMISS_EXEMPT names", () => {
    const p = paint([pointNote()]);
    expect(p.kids).toHaveLength(1);
    expect(p.kids[0]!.className).toBe("annpin");
    // BOTH SIDES OF THE CONTRACT, in one assertion: whatever the painter writes
    // has to be a selector the exemption list carries.
    expect(ANN_DISMISS_EXEMPT).toContain("." + String(p.kids[0]!.className));
  });

  test("a mousedown on a pin does NOT dismiss the composer (A14's toggle)", () => {
    const cls = String(paint([pointNote()]).kids[0]!.className);
    const pop = {
      style: { display: "block" },
      contains: () => false,
    } as unknown as HTMLElement;
    // The pin as the dismissal sees it: a node whose `closest` answers for its
    // own class (hosted, the shadow boundary retargets to the layer host, which
    // the list exempts by `[data-fused-annotate]` — this is the split case).
    const pin = {
      closest: (sel: string) => (sel === "." + cls ? pin : null),
    } as unknown as Element;
    expect(dismissesComposer(pop, pin)).toBe(false);
    // A node of the app, by contrast, does dismiss it.
    const other = { closest: () => null } as unknown as Element;
    expect(dismissesComposer(pop, other)).toBe(true);
  });

  test("a WORDLESS mark's tooltip names the action, not a bare em-dash phrase", () => {
    // A walkthrough's mark has no words until the transcript lands, and the
    // concatenation printed a bare em-dash phrase for it.
    expect(paint([pointNote()]).kids[0]!.title).toBe("make this blue — click to edit");
    expect(paint([pointNote({ content: "" })]).kids[0]!.title).toBe("Click to edit");
  });

  test("a SENT note and an earlier round's note draw no pin (T:6880)", () => {
    expect(paint([pointNote({ sent: 1 })]).kids).toHaveLength(0);
    expect(paint([pointNote({ createdAt: 500 })]).kids).toHaveLength(0);
  });
});

describe("createRenderQueue disposes (a frame must not paint after unmount)", () => {
  test("one render per frame, however many queue calls", () => {
    const frames: Array<() => void> = [];
    let painted = 0;
    const q = createRenderQueue(() => {
      painted += 1;
    }, (cb) => frames.push(cb));
    q.queue();
    q.queue();
    q.queue();
    expect(frames).toHaveLength(1);
    frames[0]!();
    expect(painted).toBe(1);
  });

  test("a frame queued BEFORE the dispose does not paint after it", () => {
    const frames: Array<() => void> = [];
    let painted = 0;
    const q = createRenderQueue(() => {
      painted += 1;
    }, (cb) => frames.push(cb));
    q.queue();
    q.dispose();
    frames[0]!(); // the scroll's frame, arriving after the teardown
    expect(painted).toBe(0);
  });

  test("dispose is not a one-way door: a re-mounted queue still paints", () => {
    // React runs an effect's cleanup between two mounts in StrictMode, so a
    // queue that could never be used again would be a dead subsystem in dev.
    const frames: Array<() => void> = [];
    let painted = 0;
    const q = createRenderQueue(() => {
      painted += 1;
    }, (cb) => frames.push(cb));
    q.dispose();
    q.queue();
    frames[0]!();
    expect(painted).toBe(1);
  });
});

describe("removeLayer takes our node out of a document we do not own", () => {
  test("the host is removed, and no host is the same outcome as one removed", () => {
    let removed = 0;
    const host = {
      shadowRoot: { querySelector: () => null },
      remove: () => {
        removed += 1;
      },
    };
    removeLayer({ querySelector: () => host } as unknown as Document);
    expect(removed).toBe(1);
    expect(() => removeLayer({ querySelector: () => null } as unknown as Document)).not.toThrow();
    expect(() => removeLayer(null)).not.toThrow();
  });

  test("a document that went first is not an error", () => {
    const host = {
      shadowRoot: null,
      remove: () => {
        throw new Error("the document went first");
      },
    };
    expect(() =>
      removeLayer({ querySelector: () => host } as unknown as Document),
    ).not.toThrow();
  });
});

// ── the cross-origin overlay ────────────────────────────────────────────────
//
// A MARKED FRAME WE CANNOT ENTER (D349): no layer injection, no anchors, just
// the frame's BOX in the parent's own document with a crosshair catcher over it.
// Nothing drove `createXOLayer` at all before, which is why its two defects were
// invisible to every other suite here: its host was never removed on unmount
// (the hosted teardown removes `[data-fused-annotate]` from the TARGET document,
// which in XO is null), leaving a `z-index: 2147483646` click-swallower over the
// shell's iframe for ever; and its bar's ResizeObserver outlived the node.

/** A node with the surface `buildBarNode` and `createXOLayer` touch. */
function fakeNode(doc: FakeDoc, tag: string): FakeEl {
  const kids: FakeEl[] = [];
  const classes = new Set<string>();
  const el: FakeEl = {
    tagName: tag.toUpperCase(),
    kids,
    attrs: {} as Record<string, string>,
    className: "",
    id: "",
    type: "",
    hidden: false,
    textContent: "",
    innerHTML: "",
    style: {} as Record<string, string>,
    dataset: {} as Record<string, string>,
    listeners: [] as Array<{ type: string; fn: (e: unknown) => void }>,
    shadowRoot: null,
    isConnected: false,
    removed: 0,
    classList: {
      add: (c: string) => classes.add(c),
      remove: (c: string) => classes.delete(c),
      contains: (c: string) => classes.has(c),
      toggle: (c: string, on?: boolean) => (on ? classes.add(c) : classes.delete(c)),
    },
    setAttribute(k: string, v: string) {
      el.attrs[k] = v;
    },
    getAttribute: (k: string) => el.attrs[k] ?? null,
    append(...ns: FakeEl[]) {
      kids.push(...ns);
    },
    appendChild(n: FakeEl) {
      kids.push(n);
      n.isConnected = true;
      return n;
    },
    addEventListener(type: string, fn: (e: unknown) => void) {
      el.listeners.push({ type, fn });
    },
    removeEventListener() {},
    getBoundingClientRect: () => ({ left: 0, top: 0, right: 0, width: 0, height: 0 }),
    remove() {
      el.removed += 1;
      el.isConnected = false;
    },
    attachShadow() {
      const root: FakeEl = fakeNode(doc, "shadow");
      root.ownerDocument = doc as unknown as Document;
      el.shadowRoot = root;
      return root;
    },
    querySelector: (sel: string) => findIn(kids, sel),
    ownerDocument: doc as unknown as Document,
  };
  return el;
}

interface FakeEl {
  [k: string]: unknown;
  kids: FakeEl[];
  className: string;
  style: Record<string, string>;
  listeners: Array<{ type: string; fn: (e: unknown) => void }>;
  shadowRoot: FakeEl | null;
  isConnected: boolean;
  removed: number;
  attrs: Record<string, string>;
}

/** `.class` / `#id` only — the two shapes the layer ever asks for. */
function findIn(kids: FakeEl[], sel: string): FakeEl | null {
  for (const k of kids) {
    const hit = sel.startsWith(".")
      ? String(k.className).split(" ").includes(sel.slice(1))
      : sel.startsWith("#")
        ? k.id === sel.slice(1)
        : false;
    if (hit) return k;
    const deep = findIn(k.kids, sel);
    if (deep) return deep;
  }
  return null;
}

interface FakeDoc {
  body: FakeEl;
  createElement(tag: string): FakeEl;
  defaultView: { ResizeObserver: unknown };
}

function parentDoc() {
  const observers: Array<{ live: boolean }> = [];
  class RO {
    private rec = { live: true };
    constructor(_cb: () => void) {
      observers.push(this.rec);
    }
    observe() {}
    disconnect() {
      this.rec.live = false;
    }
  }
  const doc = {
    createElement: (tag: string) => fakeNode(doc, tag),
    defaultView: { ResizeObserver: RO },
  } as unknown as FakeDoc;
  doc.body = fakeNode(doc, "body");
  return { doc, observers };
}

const BAR_HANDLERS = { onDone() {}, onStop() {}, onDiscard() {}, onResize() {} };

function xoWorld(over: { armed?: boolean } = {}) {
  const p = parentDoc();
  const state = { armed: over.armed ?? true, points: [] as Array<{ x: number; y: number }> };
  const frame = {
    getBoundingClientRect: () => ({ left: 40, top: 100, width: 600, height: 400 }),
  } as unknown as HTMLIFrameElement;
  const layer = createXOLayer({
    frame: () => frame,
    parentDoc: () => p.doc as unknown as Document,
    bar: BAR_HANDLERS,
    onPoint: (x, y) => state.points.push({ x, y }),
    armed: () => state.armed,
  });
  return { ...p, state, layer };
}

/** The overlay's click, as the catcher hears it. */
function clickCatcher(w: ReturnType<typeof xoWorld>, clientX = 140, clientY = 160) {
  const host = w.doc.body.kids[0]!;
  const catcher = findIn(host.kids.length ? host.kids : (host.shadowRoot?.kids ?? []), ".catch")
    ?? findIn(host.shadowRoot?.kids ?? [], ".catch");
  const seen = { prevented: 0, stopped: 0 };
  for (const l of catcher!.listeners) {
    if (l.type !== "click") continue;
    l.fn({
      clientX,
      clientY,
      preventDefault() {
        seen.prevented += 1;
      },
      stopPropagation() {
        seen.stopped += 1;
      },
    });
  }
  return seen;
}

describe("createXOLayer: the overlay in the PARENT's document (T:6294, A43)", () => {
  test("one host, marked and placed over the frame — and reused, never reflashed", () => {
    const w = xoWorld();
    const first = w.layer.resolve();
    expect(w.doc.body.kids).toHaveLength(1);
    const host = w.doc.body.kids[0]!;
    expect(host.attrs["data-fused-annotate"]).toBe("xo-layer");
    // The frame's box, in the parent's own layout — the one thing about a
    // cross-origin pane that is not walled off.
    expect(host.style.left).toBe("40px");
    expect(host.style.top).toBe("100px");
    expect(host.style.width).toBe("600px");
    expect(host.style.height).toBe("400px");
    // Every sync and every poll resolves; a second host per resolve would flash
    // a new overlay on every tick.
    const second = w.layer.resolve();
    expect(w.doc.body.kids).toHaveLength(1);
    expect(second!.root).toBe(first!.root);
    // Point notes only: the stage IS the host (there is no scroll to read), and
    // the bindings the painter needs are all present.
    expect(second!.stage).toBe(host as unknown as Element);
    expect(second!.pins).toBeTruthy();
    expect(second!.bar).toBeTruthy();
  });

  test("armed, the catcher swallows the click and reports OVERLAY coordinates", () => {
    const w = xoWorld();
    w.layer.resolve();
    const seen = clickCatcher(w, 140, 160);
    expect(seen).toEqual({ prevented: 1, stopped: 1 });
    // Overlay-relative from birth: the host's own rect is the origin.
    expect(w.state.points).toEqual([{ x: 140, y: 160 }]);
  });

  test("DISARMED, it eats nothing — the reader is USING the framed app", () => {
    const w = xoWorld({ armed: false });
    w.layer.resolve();
    const host = w.doc.body.kids[0]!;
    // `display`, not `pointer-events`, so a hidden catcher cannot intercept a
    // drag that started while armed (T:6332).
    expect(findIn(host.shadowRoot!.kids, ".catch")!.style.display).toBe("none");
    const seen = clickCatcher(w);
    expect(seen).toEqual({ prevented: 0, stopped: 0 });
    expect(w.state.points).toEqual([]);
  });

  test("remove() takes the host out AND disconnects the bar's ResizeObserver", () => {
    const w = xoWorld();
    w.layer.resolve();
    const host = w.doc.body.kids[0]!;
    expect(w.observers).toHaveLength(1);
    expect(w.observers[0]!.live).toBe(true);

    w.layer.remove();
    expect(host.removed).toBe(1);
    // THE LEAK: removing the node does not stop the observer — it holds the
    // node, not the other way round. `removeLayer` was fixed for this; the XO
    // path was the one that was not.
    expect(w.observers[0]!.live).toBe(false);
    // Idempotent, and a parent torn down first is not an error.
    expect(() => w.layer.remove()).not.toThrow();
    expect(host.removed).toBe(1);
  });

  test("a host the parent re-rendered away is rebuilt, not trusted", () => {
    const w = xoWorld();
    w.layer.resolve();
    const first = w.doc.body.kids[0]!;
    first.isConnected = false; // the parent dropped our node
    w.layer.resolve();
    expect(first.removed).toBe(1);
    expect(w.doc.body.kids).toHaveLength(2); // the fake body keeps its history
    expect(w.doc.body.kids[1]!.isConnected).toBe(true);
  });

  test("no frame, or no parent document, is null and builds nothing", () => {
    const p = parentDoc();
    const none = createXOLayer({
      frame: () => null,
      parentDoc: () => p.doc as unknown as Document,
      bar: BAR_HANDLERS,
      onPoint: () => {},
      armed: () => true,
    });
    expect(none.resolve()).toBeNull();
    expect(p.doc.body.kids).toHaveLength(0);
    expect(() => none.remove()).not.toThrow();
  });
});

describe("the layer's stylesheet declares its OWN palette (QA round 2, item 4)", () => {
  test(":host defines every bar token, so the app's cannot leak in", () => {
    // `all: initial` does not reset custom properties, so an app with its own
    // `--accent` was colouring our picker: the hosted Element/Point toggle read
    // lime while the split layout's read the shell's orange.
    for (const [, write] of ANN_BAR_TOKENS) {
      expect(ANN_LAYER_CSS).toContain(write + ":");
    }
  });

  test("the bar's tokens are READ from this page's `--c-` names", () => {
    // The list used to be the unprefixed names on both sides, so every read off
    // the shell's root found nothing and no theme was ever copied.
    for (const [read] of ANN_BAR_TOKENS) expect(read.startsWith("--c-")).toBe(true);
  });
});
