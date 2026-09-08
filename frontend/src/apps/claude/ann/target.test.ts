// THE ADOPTION — and the three bugs no other suite in this directory could see.
//
// `wire-target.test.ts` proves the seven listeners by calling `wireTarget` itself.
// That is exactly why it passed while the feature was dead in the browser: the
// only real CALLER is `createAnnTarget`, and it (a) set the `__fusedAnnWired`
// guard one line before calling in, so `wireTarget`'s own idempotence check
// fired on the first call and returned a no-op with nothing attached, (b) threw
// the teardown away, so a remount stacked a second set of capture-phase click
// swallowers, and (c) never removed the frame's `load` listener while clearing
// the flag that guarded it, so the next mount bound another one.
//
// So this suite drives the target the way the coordinator does — a fake frame
// holding a fake document, `wireDoc` pointed at the real `wireTarget` — and
// asserts through the LISTENER LIST and through a real dispatched click, which
// is the only evidence that distinguishes "wired" from "believes it is wired".
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();

import { describe, expect, test } from "bun:test";

const { createAnnTarget } = await import("./target");
const { isWired, wireTarget } = await import("./wire-target");
import type { AnnAnchor } from "./types";

/** The seven, in the order `wireTarget` attaches them. */
const SEVEN = [
  "keydown",
  "pointerdown:capture",
  "mousedown:capture",
  "keyup",
  "mousemove",
  "click:capture",
  "scroll:capture",
];

/** ONE body object shared by the fake document and its elements: `pathOf` walks
 *  `parentElement` until it reaches `doc.body`, and two objects both spelled
 *  "BODY" is a walk that never arrives. */
const BODY = { tagName: "BODY", nodeType: 1 };

interface Listener {
  type: string;
  fn: (e: unknown) => void;
  capture: boolean;
}

function isCapture(opt: unknown): boolean {
  if (opt === true) return true;
  if (!opt || typeof opt !== "object") return false;
  return !!(opt as { capture?: boolean }).capture;
}

function registry() {
  const listeners: Listener[] = [];
  return {
    listeners,
    /** The listener list as `type[:capture]`, which is what the assertions read. */
    names: () => listeners.map((l) => l.type + (l.capture ? ":capture" : "")),
    add(type: string, fn: (e: unknown) => void, opt?: unknown) {
      listeners.push({ type, fn, capture: isCapture(opt) });
    },
    remove(type: string, fn: (e: unknown) => void, opt?: unknown) {
      const capture = isCapture(opt);
      const i = listeners.findIndex(
        (l) => l.type === type && l.fn === fn && l.capture === capture,
      );
      if (i !== -1) listeners.splice(i, 1);
    },
    fire(type: string, e: Record<string, unknown>) {
      for (const l of [...listeners]) if (l.type === type) l.fn(e);
    },
  };
}

/** An element with a resolvable path: one child of body. */
function child(id?: string) {
  return {
    tagName: "BUTTON",
    id,
    nodeType: 1,
    textContent: "Send",
    children: [],
    previousElementSibling: null,
    parentElement: BODY,
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 10, height: 10 }),
    closest: () => null,
  } as unknown as Element;
}

interface Rig {
  /** Every `openComposer` this instance saw — the proof the click ARRIVED. */
  opened: Array<{ x: number; y: number; anchor: AnnAnchor }>;
  marked: AnnAnchor[];
  renders: number;
  queued: number;
}

/**
 * One frame, one document, one target — assembled the way `useAnnotations` does
 * it, including the real `wireTarget` behind `wireDoc`.
 *
 * `frames` and `docs` are the registries, so a test can count what is attached
 * to each rather than trust either object's own bookkeeping.
 */
function world(opts: { hosted?: boolean } = {}) {
  const docs = registry();
  const doc = {
    body: BODY,
    documentElement: { style: { cursor: "" } as { cursor: string } },
    defaultView: { scrollX: 0, scrollY: 0 } as unknown as Window,
    querySelector: () => null,
    addEventListener: docs.add,
    removeEventListener: docs.remove,
  } as unknown as Document;

  const frames = registry();
  const frame = {
    tagName: "IFRAME",
    contentDocument: doc,
    contentWindow: { location: { href: "http://app.test/index.html" } },
    addEventListener: frames.add,
    removeEventListener: frames.remove,
  } as unknown as HTMLIFrameElement;

  const rig: Rig = { opened: [], marked: [], renders: 0, queued: 0 };

  const make = () =>
    createAnnTarget({
      hosted: !!opts.hosted,
      noPane: () => false,
      markedFrame: () => frame,
      queueRender: () => {
        rig.queued += 1;
      },
      render: () => {
        rig.renders += 1;
      },
      wireDoc: (d) =>
        wireTarget(d, {
          armed: () => true,
          recording: () => false,
          tool: () => "element",
          composerOpen: () => false,
          hl: () => null,
          onEscape: () => {},
          closeComposer: () => {},
          openComposer: (x, y, anchor) => {
            rig.opened.push({ x, y, anchor });
          },
          markPoint: () => {},
          mark: (anchor) => {
            rig.marked.push(anchor);
          },
          queueRender: () => {
            rig.queued += 1;
          },
        }),
      document: doc,
      win: {
        setInterval: () => 1 as unknown as number,
        clearInterval: () => {},
        addEventListener: () => {},
        removeEventListener: () => {},
      } as unknown as Window,
    });

  return { doc, docs, frame, frames, rig, make };
}

/** A real dispatched click on the framed document, capture phase — the gesture
 *  the whole feature is. */
function clickIn(w: ReturnType<typeof world>, el: Element = child("send")): void {
  w.docs.fire("click", {
    target: el,
    clientX: 40,
    clientY: 60,
    altKey: false,
    preventDefault() {},
    stopPropagation() {},
  });
}

describe("createAnnTarget adopts the framed document (T:8490-8530)", () => {
  test("start() ATTACHES the seven listeners — the guard is wireTarget's alone", () => {
    const w = world();
    w.make().start();
    // The blocker: this list was EMPTY in every layout, because the guard was
    // written by the caller before the call.
    expect(w.docs.names()).toEqual(SEVEN);
    expect(isWired(w.doc)).toBe(true);
  });

  test("a real click in the framed document reaches openComposer", () => {
    const w = world();
    w.make().start();
    clickIn(w);
    expect(w.rig.opened).toHaveLength(1);
    expect(w.rig.opened[0]!.anchor.anchorId).toBe("send");
    expect(w.rig.opened[0]!.x).toBe(40);
  });

  test("one `load` listener per frame, and the document is wired ONCE", () => {
    const w = world();
    w.make().start();
    expect(w.frames.names()).toEqual(["load"]);
    // A load that brings the SAME document back (the mark leaving and returning
    // as the reader switches the pane's mode) must not stack a second set.
    w.frames.fire("load", {});
    w.frames.fire("load", {});
    expect(w.docs.names()).toEqual(SEVEN);
    clickIn(w);
    expect(w.rig.opened).toHaveLength(1); // not three
  });

  test("the teardown takes all seven off and releases BOTH guards", () => {
    const w = world();
    const stop = w.make().start();
    stop();
    expect(w.docs.names()).toEqual([]);
    expect(w.frames.names()).toEqual([]);
    expect(isWired(w.doc)).toBe(false);
    // And the released document is inert: a click that arrives after the
    // teardown reaches nothing.
    clickIn(w);
    expect(w.rig.opened).toHaveLength(0);
  });

  test("releaseGuards() alone is a full release — listeners included", () => {
    const w = world();
    const t = w.make();
    t.start();
    t.releaseGuards();
    expect(w.docs.names()).toEqual([]);
    expect(w.frames.names()).toEqual([]);
    expect(isWired(w.doc)).toBe(false);
  });

  test("a REMOUNT wires seven again, not fourteen, and only the live instance hears", () => {
    const w = world();
    const first = w.make();
    const stop = first.start();
    stop();

    const second = w.make();
    second.start();
    // The second blocker: the teardown was discarded while the guards were
    // cleared, so this list was the seven twice over — every click swallowed
    // twice, by one live instance and one dead one.
    expect(w.docs.names()).toEqual(SEVEN);
    expect(w.frames.names()).toEqual(["load"]);

    clickIn(w);
    expect(w.rig.opened).toHaveLength(1);
  });

  test("hosted: the poll adopts the frame and re-polling stacks nothing", () => {
    const w = world({ hosted: true });
    const t = w.make();
    const stop = t.start(); // `start` polls once
    expect(w.docs.names()).toEqual(SEVEN);
    t.poll();
    t.poll();
    expect(w.docs.names()).toEqual(SEVEN);
    expect(w.frames.names()).toEqual(["load"]);
    clickIn(w);
    expect(w.rig.opened).toHaveLength(1);
    stop();
    expect(w.docs.names()).toEqual([]);
    expect(w.frames.names()).toEqual([]);
  });

  test("split: a frame that ARRIVES after start() is still adopted", () => {
    // The pane is `AppPane`'s, mounted after this component's effects: `start()`
    // finds nothing, and the only thing that runs afterwards is `sync()` (from
    // the paint, the binds, `onFrameLoad`). It used to adopt nothing in this
    // layout, so a split chat had a marked frame, a painted bar and no listeners
    // at all inside the app.
    const docs = registry();
    const doc = {
      body: BODY,
      documentElement: { style: { cursor: "" } as { cursor: string } },
      defaultView: { scrollX: 0, scrollY: 0 } as unknown as Window,
      querySelector: () => null,
      addEventListener: docs.add,
      removeEventListener: docs.remove,
    } as unknown as Document;
    const frames = registry();
    let live: HTMLIFrameElement | null = null;
    const frame = {
      tagName: "IFRAME",
      contentDocument: doc,
      contentWindow: { location: { href: "http://app.test/index.html" } },
      addEventListener: frames.add,
      removeEventListener: frames.remove,
    } as unknown as HTMLIFrameElement;

    const opened: number[] = [];
    const t = createAnnTarget({
      hosted: false,
      noPane: () => false,
      markedFrame: () => live,
      queueRender: () => {},
      render: () => {},
      wireDoc: (d) =>
        wireTarget(d, {
          armed: () => true,
          recording: () => false,
          tool: () => "element",
          composerOpen: () => false,
          hl: () => null,
          onEscape: () => {},
          closeComposer: () => {},
          openComposer: (x) => {
            opened.push(x);
          },
          markPoint: () => {},
          mark: () => {},
          queueRender: () => {},
        }),
      document: doc,
    });
    const stop = t.start();
    expect(docs.names()).toEqual([]); // nothing marked yet

    live = frame; // `AppPane` stamps its iframe
    t.sync();
    expect(docs.names()).toEqual(SEVEN);
    expect(frames.names()).toEqual(["load"]);
    // And a further sync per paint stacks nothing.
    t.sync();
    t.sync();
    expect(docs.names()).toEqual(SEVEN);

    docs.fire("click", {
      target: child("go"),
      clientX: 7,
      clientY: 8,
      altKey: false,
      preventDefault() {},
      stopPropagation() {},
    });
    expect(opened).toEqual([7]);

    stop();
    expect(docs.names()).toEqual([]);
    expect(frames.names()).toEqual([]);
  });

  test("the scroll listener is passive-capture and queues one repaint", () => {
    const w = world();
    w.make().start();
    const before = w.rig.queued;
    w.docs.fire("scroll", {});
    expect(w.rig.queued).toBe(before + 1);
  });
});

describe("the guards and the layers a torn-down instance leaves behind", () => {
  test("a STALE `__fusedAnnWired` from a dead instance does not silence the next", () => {
    // `wired` is our own record; the expando is a document expando whose only
    // clearer is our own teardown. An instance that never got to tear down (a
    // crash, a host that dropped the tree without a `pagehide`, or two chat
    // trees over one host frame) leaves it set — and `wireTarget` then returns
    // its no-op with nothing attached. That is round 1's dead-click blocker,
    // reached without a remount and with no console error.
    const w = world();
    (w.doc as unknown as { __fusedAnnWired?: boolean }).__fusedAnnWired = true;
    w.make().start();
    expect(w.docs.names()).toEqual(SEVEN);
    clickIn(w);
    expect(w.rig.opened).toHaveLength(1);
  });

  test("removeInjectedLayer takes the XO overlay too — the parent's document", () => {
    // XO IS the hosted layout, so the only teardown that runs for it is the
    // hosted one, and it calls exactly this. Both of the documents it removes
    // from are null in XO (`sync` retargets the layer to null), so without this
    // seam the overlay — `position: fixed`, `z-index: 2147483646`, a crosshair
    // click-swallower over the shell's iframe — was never removed at all.
    const docs = registry();
    const doc = {
      body: BODY,
      documentElement: { style: { cursor: "" } as { cursor: string } },
      defaultView: { scrollX: 0, scrollY: 0 } as unknown as Window,
      querySelector: () => null,
      addEventListener: docs.add,
      removeEventListener: docs.remove,
    } as unknown as Document;
    let removedXO = 0;
    const t = createAnnTarget({
      hosted: true,
      noPane: () => false,
      markedFrame: () => null,
      queueRender: () => {},
      render: () => {},
      wireDoc: () => undefined,
      removeXOLayer: () => {
        removedXO += 1;
      },
      document: doc,
    });
    t.removeInjectedLayer();
    expect(removedXO).toBe(1);
  });

  test("one adoption is ONE paint: the load reached from a sync does not re-render", () => {
    // `render → sync → watch → onFrameLoad → render` was two `resolveLayer`
    // round trips and two full paints per adoption, with `render()`'s own
    // `sync()` making every render a re-entry point.
    const w = world({ hosted: true });
    const t = w.make();
    t.start(); // start() polls, which syncs, which adopts and wires
    const afterAdoption = w.rig.renders;
    // A real load — the app live-reloading on Claude's edits — still paints.
    w.frames.fire("load", {});
    expect(w.rig.renders).toBe(afterAdoption + 1);
    // …and the sync's own adoption paid one paint, not two.
    expect(afterAdoption).toBeLessThanOrEqual(1);
  });
});
