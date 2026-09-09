// THE COORDINATOR, DRIVEN BY A CLICK — the one path every other suite in this
// directory stops short of.
//
// `mode.test.ts` proves the transitions, `store.test.ts` the round, `geometry`
// the arithmetic and `wire-target.test.ts` the seven listeners against a document
// it wires itself. None of them joins the two ends: a click that lands in the
// framed app, travels through the listeners the hook attached, opens the
// composer with the anchor the click built, and comes back as a chip.
//
// So the hook is mounted for real (`react-test-renderer`) over a fake frame
// holding a fake document with a working listener registry, and the gesture is
// DISPATCHED rather than simulated by calling the hook's own seams. That is the
// difference that matters: the blockers this suite exists for were all in the
// wiring between those two ends, and every assertion here fails without them.
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();

import { beforeEach, expect, test } from "bun:test";
import { act, create } from "react-test-renderer";

const { useAnnotations, seatsAria } = await import("./useAnnotations");
const { createMemoryParamsStore } = await import("../params/store");
import type { AnnotationsApi } from "./useAnnotations";

/** ONE body object shared by the fake document and its elements: `pathOf` walks
 *  `parentElement` until it reaches `doc.body`. */
const BODY = { tagName: "BODY", nodeType: 1 };

interface Listener {
  type: string;
  fn: (e: unknown) => void;
}

function registry() {
  const listeners: Listener[] = [];
  return {
    listeners,
    add(type: string, fn: (e: unknown) => void) {
      listeners.push({ type, fn });
    },
    remove(type: string, fn: (e: unknown) => void) {
      const i = listeners.findIndex((l) => l.type === type && l.fn === fn);
      if (i !== -1) listeners.splice(i, 1);
    },
    fire(type: string, e: Record<string, unknown>) {
      for (const l of [...listeners]) if (l.type === type) l.fn(e);
    },
  };
}

/** Enough of an element for `buildToolNode` and the doors. */
function elem(tag: string): Record<string, unknown> {
  const classes = new Set<string>();
  const node: Record<string, unknown> = {
    tagName: tag.toUpperCase(),
    nodeType: 1,
    id: "",
    type: "",
    hidden: false,
    innerHTML: "",
    textContent: "",
    className: "",
    dataset: {},
    style: {} as Record<string, string>,
    children: [] as unknown[],
    classList: {
      add: (c: string) => classes.add(c),
      remove: (c: string) => classes.delete(c),
      contains: (c: string) => classes.has(c),
    },
    setAttribute: () => {},
    append: (...kids: unknown[]) => {
      (node.children as unknown[]).push(...kids);
    },
    appendChild: (kid: unknown) => {
      (node.children as unknown[]).push(kid);
    },
    querySelector: () => null,
    addEventListener: () => {},
    removeEventListener: () => {},
  };
  return node;
}

/** The composer node, hand-built: the real one is `AnnPopover`'s imperative
 *  node, which needs a DOM to build. What the placement and the commit touch is
 *  the textarea, the style and the classList. */
function popNode(ownDoc: Document) {
  const ta = { value: "", placeholder: "", focus: () => {}, setSelectionRange: () => {} };
  const classes = new Set<string>();
  const pop = {
    // OWNERSHIP is what `isPortaled` asks: a card whose owner IS the chat's
    // document is at home, which is where the split layout's card always is.
    ownerDocument: ownDoc,
    style: { display: "none" } as Record<string, string>,
    classList: {
      add: (c: string) => classes.add(c),
      remove: (c: string) => classes.delete(c),
      contains: (c: string) => classes.has(c),
    },
    querySelector: (sel: string) => (sel === "textarea" ? ta : null),
    contains: () => false,
    getRootNode: () => null,
  };
  return { pop: pop as unknown as HTMLElement, ta };
}

/** The chat's own document — one we can build nodes in, which is what makes the
 *  hook treat it as a document at all. */
function ownDocument() {
  const own = registry();
  const root = elem("html");
  (root.style as Record<string, unknown>).setProperty = () => {};
  (root.style as Record<string, unknown>).removeProperty = () => {};
  const doc = {
    body: elem("body"),
    documentElement: root,
    createElement: (tag: string) => elem(tag),
    querySelector: () => null,
    addEventListener: own.add,
    removeEventListener: own.remove,
    // No MutationObserver and no ResizeObserver: this document is the CHAT's,
    // and the two effects that want them degrade to doing nothing, which is the
    // documented answer for a host without them.
    defaultView: {
      addEventListener: () => {},
      removeEventListener: () => {},
    } as unknown as Window,
  };
  return { doc: doc as unknown as Document, own };
}

/** The framed app: a document with a real listener registry, in a frame whose
 *  `load` already fired (`about:blank` would mean "the first one is coming"). */
function framed() {
  const docs = registry();
  const doc = {
    body: BODY,
    documentElement: { style: { cursor: "" } as { cursor: string } },
    defaultView: { scrollX: 0, scrollY: 0 } as unknown as Window,
    querySelector: () => null,
    // The paint RESOLVES an element anchor back to its element, so the fake
    // document has to be able to answer that — otherwise the pin has no box.
    getElementById: (id: string) => appButton(id, id === "send" ? "Send" : id),
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
  return { doc, docs, frame, frames };
}

/** A button in the app, with a resolvable id. */
function appButton(id = "send", text = "Send") {
  return {
    tagName: "BUTTON",
    id,
    nodeType: 1,
    textContent: text,
    children: [],
    previousElementSibling: null,
    parentElement: BODY,
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 40, height: 20 }),
    closest: () => null,
  } as unknown as Element;
}

// ── the mount ───────────────────────────────────────────────────────────────

let api: AnnotationsApi | null = null;
const mounted: Array<ReturnType<typeof create>> = [];

function Probe(props: {
  frame: HTMLIFrameElement;
  ownDoc: Document;
  pop: HTMLElement;
  autoSubmits: { n: number };
}) {
  const [params] = [PARAMS];
  const ann = useAnnotations({
    params,
    hosted: false,
    noPane: false,
    annotateTarget: () => props.frame,
    canSend: () => true,
    autoSubmit: () => {
      props.autoSubmits.n += 1;
    },
    document: props.ownDoc,
    raf: (cb) => cb(),
  });
  api = ann;
  ann.bindPop(props.pop);
  return null;
}

let PARAMS = createMemoryParamsStore();

beforeEach(() => {
  PARAMS = createMemoryParamsStore();
  api = null;
});

function mount() {
  const f = framed();
  const o = ownDocument();
  const p = popNode(o.doc);
  const autoSubmits = { n: 0 };
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(
      <Probe frame={f.frame} ownDoc={o.doc} pop={p.pop} autoSubmits={autoSubmits} />,
    );
  });
  mounted.push(r);
  return { ...f, ...o, ...p, autoSubmits, r };
}

/** The real gesture: a capture-phase click in the framed document. */
function clickApp(w: ReturnType<typeof mount>, el: Element = appButton()): void {
  act(() => {
    w.docs.fire("click", {
      target: el,
      clientX: 120,
      clientY: 90,
      altKey: false,
      preventDefault() {},
      stopPropagation() {},
    });
  });
}

function pressEscapeInApp(w: ReturnType<typeof mount>): void {
  act(() => {
    w.docs.fire("keydown", { key: "Escape", preventDefault() {} });
  });
}

function arm(): void {
  act(() => api!.arm());
}

// ── the tests ───────────────────────────────────────────────────────────────

test("the hook WIRES the framed document — seven listeners, from the mount", () => {
  const w = mount();
  expect(w.docs.listeners.map((l) => l.type)).toEqual([
    "keydown",
    "pointerdown",
    "mousedown",
    "keyup",
    "mousemove",
    "click",
    "scroll",
  ]);
  expect(w.frames.listeners.map((l) => l.type)).toEqual(["load"]);
});

test("a REAL click in the framed app opens the composer on the clicked element", () => {
  const w = mount();
  arm();
  expect(api!.mode).toBe("comment");
  clickApp(w);
  // The composer opened, aimed, with the anchor the click built.
  expect(w.pop.style.display).toBe("block");
  expect(w.ta.placeholder).toBe("What about this element?");
  expect(w.ta.value).toBe("");
});

test("click → type → commit is a note, and the note is a chip", () => {
  const w = mount();
  arm();
  clickApp(w);
  w.ta.value = "make this blue";
  act(() => api!.popHandlers.commit(w.ta.value));

  expect(api!.annotations).toHaveLength(1);
  const note = api!.annotations[0]!;
  expect(note.content).toBe("make this blue");
  // The ANCHOR is the click's, not a stand-in: the element's id, its tag and
  // the 80-char digest the `<annotations>` stanza prints.
  expect(note.anchorId).toBe("send");
  expect(note.tag).toBe("button");
  expect(note.text).toBe("Send");
  expect(api!.chips).toHaveLength(1);
  expect(api!.chips[0]!.label).toBe("A");
  expect(api!.chips[0]!.note.content).toBe("make this blue");
  // The composer went home on the commit.
  expect(w.pop.style.display).toBe("none");
});

test("the committed note PAINTS ITS PIN, with no other trigger", () => {
  // The chips are React's and follow the list for free. The pins are the
  // painter's imperative nodes, and a store write is the only event that says
  // the list changed — so the write has to ask for the frame, or a note gets its
  // chip and no pin until an unrelated resize or scroll repaints.
  const w = mount();
  const kids: Array<Record<string, unknown>> = [];
  const pins = {
    innerHTML: "",
    style: {} as Record<string, string>,
    appendChild: (n: Record<string, unknown>) => {
      kids.push(n);
    },
    ownerDocument: {
      createElement: () => ({
        className: "",
        style: {} as Record<string, string>,
        textContent: "",
        title: "",
        onclick: null,
      }),
    },
  } as unknown as HTMLElement;
  const hl = { style: {} as Record<string, string> } as unknown as HTMLElement;
  // The stage takes CHILDREN, because the split layout's composer stands in it:
  // the card's left/top are framed-viewport pixels, so its containing block has
  // to be the box those are measured against (QA round 2, item 3).
  const stage = {
    clientWidth: 800,
    clientHeight: 600,
    appendChild: () => {},
  } as unknown as HTMLElement;
  act(() => api!.bindPins({ pins, hl, stage }));

  arm();
  clickApp(w);
  act(() => api!.popHandlers.commit("make this blue"));

  expect(kids).toHaveLength(1);
  expect(kids[0]!.className).toBe("annpin");
  expect(kids[0]!.textContent).toBe("A");
});

test("the Point tool pins the SPOT, in page coordinates", () => {
  const w = mount();
  arm();
  act(() => api!.setTool("point"));
  clickApp(w);
  w.ta.value = "too much space here";
  act(() => api!.popHandlers.commit(w.ta.value));

  const note = api!.annotations[0]!;
  expect(note.kind).toBe("point");
  expect(note.x).toBe(120);
  expect(note.y).toBe(90);
  // A forced point over a real element NAMES it — as a hint, never the anchor.
  expect(note.nearPath).toBe("#send");
  expect(note.anchorId).toBeUndefined();
  expect(api!.chips[0]!.point).toBe(true);
});

test("Alt overrides the tool for ONE click, in both directions", () => {
  const w = mount();
  arm();
  act(() => {
    w.docs.fire("click", {
      target: appButton(),
      clientX: 10,
      clientY: 10,
      altKey: true, // Element tool + Alt = a point
      preventDefault() {},
      stopPropagation() {},
    });
  });
  act(() => api!.popHandlers.commit("here"));
  expect(api!.annotations[0]!.kind).toBe("point");
});

test("a click while a walkthrough RECORDS is a stamped mark, with no composer", () => {
  const f = framed();
  const o = ownDocument();
  const p = popNode(o.doc);
  function Rec() {
    const ann = useAnnotations({
      params: PARAMS,
      hosted: false,
      noPane: false,
      annotateTarget: () => f.frame,
      canSend: () => true,
      autoSubmit: () => {},
      recorder: () => ({
        recording: () => true,
        settling: () => false,
        end: () => {},
        discard: () => {},
      }),
      document: o.doc,
      raf: (cb) => cb(),
    });
    api = ann;
    ann.bindPop(p.pop);
    return null;
  }
  act(() => {
    mounted.push(create(<Rec />));
  });
  // The mic arms the mode (T:7860) — every one of the seven handlers gates on
  // `armed()`, recording or not.
  act(() => api!.arm());
  act(() => {
    f.docs.fire("click", {
      target: appButton("freq", "Frequency"),
      clientX: 5,
      clientY: 5,
      altKey: false,
      preventDefault() {},
      stopPropagation() {},
    });
  });
  // A34/A31: the click IS the note, immediately — nothing grabs focus while the
  // reader is talking.
  expect(api!.annotations).toHaveLength(1);
  expect(api!.annotations[0]!.content).toBe("");
  expect(api!.annotations[0]!.anchorId).toBe("freq");
  expect(p.pop.style.display).toBe("none");
});

// The mic prompt's own window: `recording()` is already true (the mode wears
// its recording face) but the recorder has no clock yet, so its mark writer
// declines. The click is the WALKTHROUGH's — dropped, not written as a typed
// note under a bar that says "Voice annotation" (Bugbot, PR #1074).
test("a click inside the START window mints nothing at all", () => {
  const f = framed();
  const o = ownDocument();
  const p = popNode(o.doc);
  function Starting() {
    const ann = useAnnotations({
      params: PARAMS,
      hosted: false,
      noPane: false,
      annotateTarget: () => f.frame,
      canSend: () => true,
      autoSubmit: () => {},
      recorder: () => ({
        recording: () => true, // "starting" counts as recording
        settling: () => false,
        end: () => {},
        discard: () => {},
      }),
      // What `ann/rec.ts` answers before the mic arrives: nothing to stamp.
      recMark: () => null,
      recMarkPoint: () => null,
      document: o.doc,
      raf: (cb) => cb(),
    });
    api = ann;
    ann.bindPop(p.pop);
    return null;
  }
  act(() => {
    mounted.push(create(<Starting />));
  });
  act(() => api!.arm());
  act(() => {
    f.docs.fire("click", {
      target: appButton("freq", "Frequency"),
      clientX: 5,
      clientY: 5,
      altKey: false,
      preventDefault() {},
      stopPropagation() {},
    });
  });
  expect(api!.annotations).toHaveLength(0);
  expect(p.pop.style.display).toBe("none"); // and no composer either
});

test("Escape inside the FRAMED document closes the composer, then discards", () => {
  const w = mount();
  arm();
  clickApp(w);
  w.ta.value = "make this blue";
  act(() => api!.popHandlers.commit(w.ta.value));
  expect(api!.annotations).toHaveLength(1);

  // Second note, left OPEN — the first Escape is the composer's.
  clickApp(w, appButton("other", "Other"));
  expect(w.pop.style.display).toBe("block");
  pressEscapeInApp(w);
  expect(w.pop.style.display).toBe("none");
  expect(api!.annotations).toHaveLength(1); // the open draft was never committed

  // The second is the MODE's, and in a typed round leaving is discarding.
  pressEscapeInApp(w);
  expect(api!.mode).toBe("off");
  expect(api!.annotations).toHaveLength(0);
  expect(api!.chips).toHaveLength(0);
});

test("Done commits the open draft and sends the round", async () => {
  const w = mount();
  arm();
  clickApp(w);
  w.ta.value = "rename this";
  // `done()` awaits the commit (one at a time, Bugbot #664), so the assertions
  // wait for it the way the button's own handler does.
  await act(async () => {
    await api!.done();
  });
  expect(api!.annotations).toHaveLength(1);
  expect(api!.annotations[0]!.content).toBe("rename this");
  expect(w.autoSubmits.n).toBe(1);
  expect(api!.mode).toBe("off");
});

test("the UNMOUNT leaves the framed document as it found it", () => {
  const w = mount();
  arm();
  act(() => mounted.pop()!.unmount());
  expect(w.docs.listeners).toHaveLength(0);
  expect(w.frames.listeners).toHaveLength(0);
  // And a click that arrives after it reaches nothing at all.
  act(() => {
    w.docs.fire("click", {
      target: appButton(),
      clientX: 1,
      clientY: 1,
      altKey: false,
      preventDefault() {},
      stopPropagation() {},
    });
  });
  expect(api!.annotations).toHaveLength(0);
});

test("the nav lock is HANDED BACK when the target goes away while armed", () => {
  const locks: boolean[] = [];
  const f = framed();
  const o = ownDocument();
  const p = popNode(o.doc);
  let noPane = false;
  function Gone() {
    const ann = useAnnotations({
      params: PARAMS,
      hosted: false,
      noPane,
      annotateTarget: () => f.frame,
      canSend: () => true,
      autoSubmit: () => {},
      onLock: (l) => locks.push(l),
      document: o.doc,
      raf: (cb) => cb(),
    });
    api = ann;
    ann.bindPop(p.pop);
    return null;
  }
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(<Gone />);
  });
  mounted.push(r);
  act(() => api!.arm());
  expect(api!.locked).toBe(true);

  // `enterNoPane`: there is nothing to annotate any more.
  noPane = true;
  act(() => {
    r.update(<Gone />);
  });
  act(() => api!.setMode(false));
  // The bug: this branch returned before the lock, leaving `.chat-root.annlock`
  // on and ← Chats disabled with no way back.
  expect(locks[locks.length - 1]).toBe(false);
  expect(api!.locked).toBe(false);
});

// ── the strip's seats through a walkthrough (Bugbot, PR #1074) ──────────────

// THE COMMENT SEAT IS THE WALKTHROUGH'S UNTIL ITS WORDS LAND. `seatsAria` asked
// only whether a recording was LIVE, so through Stopping…/Transcribing… the
// seat came back with the armed `.on` ✓ Done face on it — enabled, spoken as
// available, and named "unavailable while the recording settles" by the very
// same strip.
test("`seatsAria` keeps the Comment seat inert for every state the recorder owns", () => {
  expect(seatsAria("off")).toEqual({ comment: false, annotate: false, screenshot: false });
  // A typed round: Done is exactly what the seat is for.
  expect(seatsAria("comment")).toEqual({ comment: false, annotate: true, screenshot: true });
  // The recording AND the mic prompt's own window (`mode()` calls both
  // "recording"), then both tenses of the settle.
  expect(seatsAria("recording")).toEqual({ comment: true, annotate: false, screenshot: true });
  expect(seatsAria("settling").comment).toBe(true);
  expect(seatsAria("transcribing").comment).toBe(true);
});

test("a Done reaching the seat mid-transcription sends nothing and leaves the mode alone", async () => {
  const w = mount();
  arm();
  // The wordless stamped marks a walkthrough's clicks leave behind: sendable
  // (`isSendable`), and waiting on the transcript for their words.
  act(() => {
    api!.store.add({ content: "", t: 1.2 });
  });
  act(() => api!.machine.setPhase("transcribing"));
  expect(api!.mode).toBe("transcribing");

  // The seat, and the door behind it: both refuse. Before this, the click ran
  // `done()` — the marks were auto-submitted WORDLESS and the mode disarmed
  // while the transcription was still on its way.
  act(() => api!.onCommentSeat());
  await act(async () => {
    await api!.done();
  });
  expect(w.autoSubmits.n).toBe(0);
  expect(api!.annotations[0]!.sent).toBeFalsy();
  expect(api!.mode).toBe("transcribing");

  // …and once the words have landed the seat is a Done again.
  act(() => api!.machine.setPhase(null));
  act(() => api!.onCommentSeat());
  await act(async () => {});
  expect(w.autoSubmits.n).toBe(1);
  expect(api!.mode).toBe("off");
});
