// The seven listeners: that there are seven, that a second wiring stacks none, and
// that the tool/Alt XOR decides what a click pins.
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();

import { describe, expect, test } from "bun:test";

const { isWired, wireTarget } = await import("./wire-target");
import type { AnnAnchor, AnnTool } from "./types";

/** ONE body object shared by the fake document and the fake elements: `pathOf`
 *  walks `parentElement` until it reaches `doc.body`, and two different objects
 *  spelled "BODY" is a walk that never arrives. */
const BODY = { tagName: "BODY", nodeType: 1 };

interface Listener {
  type: string;
  fn: (e: unknown) => void;
  capture: boolean;
}

/** A document with just the surface `wireTarget` touches. Built by hand rather
 *  than through a DOM library for the reason `testDomShim` exists: the pieces
 *  under test read four members, and a real DOM would hide which four. */
function fakeDoc() {
  const listeners: Listener[] = [];
  const root = { style: { cursor: "" } as { cursor: string } };
  const doc = {
    body: BODY,
    documentElement: root,
    defaultView: { scrollX: 0, scrollY: 0 } as unknown as Window,
    addEventListener(type: string, fn: (e: unknown) => void, opt?: unknown) {
      const capture = opt === true || (!!opt && typeof opt === "object" && "capture" in (opt as object) && !!(opt as { capture?: boolean }).capture);
      listeners.push({ type, fn, capture });
    },
    removeEventListener(type: string, fn: (e: unknown) => void, opt?: unknown) {
      const capture = opt === true || (!!opt && typeof opt === "object" && "capture" in (opt as object) && !!(opt as { capture?: boolean }).capture);
      const i = listeners.findIndex((l) => l.type === type && l.fn === fn && l.capture === capture);
      if (i !== -1) listeners.splice(i, 1);
    },
  } as unknown as Document;
  const fire = (type: string, e: Record<string, unknown>, capture?: boolean) => {
    for (const l of listeners) {
      if (l.type !== type) continue;
      if (capture !== undefined && l.capture !== capture) continue;
      l.fn(e);
    }
  };
  return { doc, listeners, fire, root };
}

interface Rig {
  armed: boolean;
  recording: boolean;
  /** Stopping…/Transcribing… — armed, but the clicks are over. */
  settling: boolean;
  tool: AnnTool;
  open: boolean;
  opened: Array<{ x: number; y: number; anchor: AnnAnchor }>;
  marked: AnnAnchor[];
  markedPoints: Array<{ x: number; y: number; nearPath?: string }>;
  closes: number;
  renders: number;
  escapes: number;
  hlDisplay: string;
}

function deps(state: Rig) {
  const hl = { style: { display: "" } } as unknown as HTMLElement;
  return {
    hl,
    api: {
      armed: () => state.armed,
      recording: () => state.recording,
      settling: () => state.settling,
      tool: () => state.tool,
      composerOpen: () => state.open,
      hl: () => hl,
      onEscape: () => {
        state.escapes++;
      },
      closeComposer: () => {
        state.closes++;
      },
      openComposer: (x: number, y: number, anchor: AnnAnchor) => {
        state.opened.push({ x, y, anchor });
        state.open = true;
      },
      markPoint: (x: number, y: number, _win: Window | null, nearPath?: string) => {
        state.markedPoints.push({ x, y, nearPath });
      },
      mark: (anchor: AnnAnchor) => {
        state.marked.push(anchor);
      },
      queueRender: () => {
        state.renders++;
      },
    },
  };
}

function rig(over: Partial<Rig> = {}) {
  const state: Rig = {
    armed: true,
    recording: false,
    settling: false,
    tool: "element",
    open: false,
    opened: [],
    marked: [],
    markedPoints: [],
    closes: 0,
    renders: 0,
    escapes: 0,
    hlDisplay: "",
    ...over,
  };
  const f = fakeDoc();
  const d = deps(state);
  const off = wireTarget(f.doc, d.api);
  return { ...f, state, off, hl: d.hl, api: d.api };
}

/** An element with a resolvable path: one child of body. */
function child(tag = "BUTTON", id?: string, text = "Click me") {
  const el: Record<string, unknown> = {
    tagName: tag,
    id,
    nodeType: 1,
    textContent: text,
    children: [],
    previousElementSibling: null,
    parentElement: BODY,
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 10, height: 10 }),
    closest: () => null,
  };
  return el;
}

describe("the seven listeners, and the guard (T:8524, 8534-8705)", () => {
  test("exactly seven, with pointerdown/mousedown/click/scroll in CAPTURE", () => {
    const r = rig();
    expect(r.listeners.map((l) => l.type + (l.capture ? ":capture" : ""))).toEqual([
      "keydown",
      "pointerdown:capture",
      "mousedown:capture",
      "keyup",
      "mousemove",
      "click:capture",
      "scroll:capture",
    ]);
  });

  test("a second wiring of the same document stacks nothing", () => {
    const r = rig();
    expect(isWired(r.doc)).toBe(true);
    const again = wireTarget(r.doc, r.api);
    expect(r.listeners).toHaveLength(7);
    // …and the second call's teardown is a no-op that cannot unwire the first.
    again();
    expect(r.listeners).toHaveLength(7);
    expect(isWired(r.doc)).toBe(true);
  });

  test("the teardown removes all seven and CLEARS the guard, so the next instance re-wires", () => {
    const r = rig();
    r.off();
    expect(r.listeners).toHaveLength(0);
    expect(isWired(r.doc)).toBe(false);
    wireTarget(r.doc, r.api);
    expect(r.listeners).toHaveLength(7);
  });

  test("an unwired document reads as unwired", () => {
    expect(isWired(null)).toBe(false);
    expect(isWired(fakeDoc().doc)).toBe(false);
  });
});

describe("gating on the mode", () => {
  test("a click while disarmed is the app's own", () => {
    const r = rig({ armed: false });
    let prevented = false;
    r.fire("click", { target: child(), preventDefault: () => (prevented = true), stopPropagation: () => {} });
    expect(prevented).toBe(false);
    expect(r.state.opened).toHaveLength(0);
  });

  test("a click through the SETTLE is the app's own too (Bugbot, PR #1074)", () => {
    // `armed()` stays true through Stopping…/Transcribing… — the transcription
    // belongs to this chat and holds the nav lock — while `recording()` is
    // already false. Read as Comment mode, the click was BOTH swallowed and
    // answered with a composer: the app lost the click and a card opened under
    // a bar that is hidden, in a state whose own round is already closed.
    const r = rig({ settling: true });
    let prevented = false;
    let stopped = false;
    r.fire("click", {
      target: child(),
      clientX: 5,
      clientY: 5,
      altKey: false,
      preventDefault: () => (prevented = true),
      stopPropagation: () => (stopped = true),
    });
    expect(prevented).toBe(false);
    expect(stopped).toBe(false);
    expect(r.state.opened).toHaveLength(0);
    // Nor is it a walkthrough mark: the recording is over.
    expect(r.state.marked).toHaveLength(0);
    expect(r.state.markedPoints).toHaveLength(0);
  });

  test("the POINTER's own events pass through the settle as well", () => {
    // The swallow that beats the click (a slider setting itself from the
    // pointer): gated on the same reading, or a drag through Transcribing…
    // would still be eaten with nothing to show for it.
    const r = rig({ settling: true });
    let prevented = 0;
    const ev = () => ({
      target: child(),
      preventDefault: () => (prevented += 1),
      stopPropagation: () => {},
    });
    r.fire("pointerdown", ev());
    r.fire("mousedown", ev());
    expect(prevented).toBe(0);
  });

  test("the settle takes the CROSSHAIR off the app, and leaves the dismissal", () => {
    const r = rig({ settling: true, tool: "point", open: true });
    r.fire("mousemove", { target: child(), clientX: 5, clientY: 5, altKey: false });
    expect(r.root.style.cursor).toBe("");
    // A composer left open by the arm still closes on a click outside it — that
    // is ours to do whichever way the mode reads, and only the SWALLOW moved.
    r.fire("mousedown", { target: child(), preventDefault: () => {}, stopPropagation: () => {} });
    expect(r.state.closes).toBe(1);
  });

  test("a caller with no `settling` seam gets the old reading", () => {
    // Optional dep: the hook wires it, and a bare integrator still swallows.
    const f = fakeDoc();
    const state: Rig = {
      armed: true, recording: false, settling: false, tool: "element", open: false,
      opened: [], marked: [], markedPoints: [], closes: 0, renders: 0, escapes: 0, hlDisplay: "",
    };
    const { api } = deps(state);
    const { settling: _drop, ...bare } = api;
    wireTarget(f.doc, bare);
    let prevented = false;
    f.fire("click", {
      target: child(),
      clientX: 1,
      clientY: 1,
      altKey: false,
      preventDefault: () => (prevented = true),
      stopPropagation: () => {},
    });
    expect(prevented).toBe(true);
  });

  test("armed, EVERY click is swallowed — buttons and links included", () => {
    const r = rig();
    let prevented = false;
    let stopped = false;
    r.fire("click", {
      target: child(),
      clientX: 5,
      clientY: 6,
      altKey: false,
      preventDefault: () => (prevented = true),
      stopPropagation: () => (stopped = true),
    });
    expect(prevented).toBe(true);
    expect(stopped).toBe(true);
  });

  test("a mousemove while disarmed resets the cursor it may have set", () => {
    const r = rig({ armed: false });
    r.root.style.cursor = "crosshair";
    r.fire("mousemove", { target: child(), altKey: false });
    expect(r.root.style.cursor).toBe("");
  });

  test("scroll always re-renders (the pins move with the content)", () => {
    const r = rig();
    r.fire("scroll", {});
    expect(r.state.renders).toBe(1);
  });

  test("keydown hands Escape to the chat's own claimant", () => {
    const r = rig();
    r.fire("keydown", { key: "Escape" });
    expect(r.state.escapes).toBe(1);
  });
});

describe("the aim signal: tool XOR Alt (T:8579-8612)", () => {
  test("Element tool: the ring follows the hover, no crosshair", () => {
    const r = rig();
    r.fire("mousemove", { target: child(), altKey: false });
    expect(r.root.style.cursor).toBe("");
    expect((r.hl as unknown as { style: { display: string } }).style.display).toBe("block");
  });

  test("Point tool: crosshair, and the ring goes away", () => {
    const r = rig({ tool: "point" });
    r.fire("mousemove", { target: child(), altKey: false });
    expect(r.root.style.cursor).toBe("crosshair");
    expect((r.hl as unknown as { style: { display: string } }).style.display).toBe("none");
  });

  test("Alt overrides in BOTH directions", () => {
    const el = rig();
    el.fire("mousemove", { target: child(), altKey: true });
    expect(el.root.style.cursor).toBe("crosshair");

    const pt = rig({ tool: "point" });
    pt.fire("mousemove", { target: child(), altKey: true });
    expect(pt.root.style.cursor).toBe("");
  });

  test("body and documentElement are not elements a note can point at", () => {
    const r = rig();
    r.fire("mousemove", { target: r.doc.body, altKey: false });
    expect((r.hl as unknown as { style: { display: string } }).style.display).toBe("none");
  });

  test("an OPEN composer freezes the ring — the popover and the ring are a pair", () => {
    const r = rig({ open: true });
    (r.hl as unknown as { style: { display: string } }).style.display = "block";
    r.fire("mousemove", { target: child(), altKey: false });
    expect((r.hl as unknown as { style: { display: string } }).style.display).toBe("block");
  });

  test("Alt's keyup re-derives the cursor, and strips the ring it brought back", () => {
    const r = rig({ tool: "point" });
    (r.hl as unknown as { style: { display: string } }).style.display = "block";
    r.fire("keyup", { key: "Alt" });
    expect(r.root.style.cursor).toBe("crosshair");
    expect((r.hl as unknown as { style: { display: string } }).style.display).toBe("none");
  });

  test("…but not off an OPEN composer's element (Point+Alt is how it got pinned)", () => {
    const r = rig({ tool: "point", open: true });
    (r.hl as unknown as { style: { display: string } }).style.display = "block";
    r.fire("keyup", { key: "Alt" });
    expect((r.hl as unknown as { style: { display: string } }).style.display).toBe("block");
  });

  test("any other key's keyup is not ours", () => {
    const r = rig({ tool: "point" });
    r.fire("keyup", { key: "a" });
    expect(r.root.style.cursor).toBe("");
  });
});

describe("what a click pins (T:8616-8698)", () => {
  const clickOn = (
    r: ReturnType<typeof rig>,
    target: unknown,
    over: Record<string, unknown> = {},
  ) =>
    r.fire("click", {
      target,
      clientX: 30,
      clientY: 40,
      altKey: false,
      preventDefault: () => {},
      stopPropagation: () => {},
      ...over,
    });

  test("an element with an id anchors by id, and carries tag + text digest", () => {
    const r = rig();
    clickOn(r, child("BUTTON", "save", "  Save   the   file  "));
    expect(r.state.opened[0].anchor).toEqual({
      anchorId: "save",
      tag: "button",
      text: "Save the file",
    });
  });

  test("no id anchors by path", () => {
    const r = rig();
    clickOn(r, child("SECTION", undefined, "hi"));
    expect(r.state.opened[0].anchor.anchorPath).toBe("section:nth-of-type(1)");
  });

  test("an 80-character digest, and no `text` key when there is none", () => {
    const r = rig();
    clickOn(r, child("P", "p", "x".repeat(200)));
    expect(r.state.opened[0].anchor.text).toHaveLength(80);
    const bare = rig();
    clickOn(bare, child("P", "p", "   "));
    expect("text" in bare.state.opened[0].anchor).toBe(false);
  });

  test("nothing under the click is a POINT note in page coordinates", () => {
    const r = rig();
    clickOn(r, r.doc.body);
    expect(r.state.opened[0].anchor).toEqual({ kind: "point", x: 30, y: 40 });
  });

  test("an element we cannot NAME is a point note too — no note could resolve it", () => {
    const r = rig();
    const orphan = { ...child("DIV"), parentElement: undefined };
    // A node whose walk never reaches <body>: `pathOf` answers null.
    clickOn(r, { ...orphan, parentElement: null });
    expect(r.state.opened[0].anchor.kind).toBe("point");
  });

  test("a FORCED point over a real element carries `nearPath` — a hint, never the anchor", () => {
    const byId = rig({ tool: "point" });
    clickOn(byId, child("BUTTON", "save"));
    expect(byId.state.opened[0].anchor).toEqual({
      kind: "point",
      x: 30,
      y: 40,
      nearPath: "#save",
    });
    expect(byId.state.opened[0].anchor.anchorId).toBeUndefined();

    const byPath = rig();
    clickOn(byPath, child("SECTION"), { altKey: true });
    expect(byPath.state.opened[0].anchor.nearPath).toBe("section:nth-of-type(1)");
  });

  test("Point tool + Alt is the element again", () => {
    const r = rig({ tool: "point" });
    clickOn(r, child("BUTTON", "save"), { altKey: true });
    expect(r.state.opened[0].anchor.anchorId).toBe("save");
  });

  test("a pin of OURS reaches its own handler rather than anchoring a new note", () => {
    const r = rig();
    const ourPin = { ...child("DIV"), closest: () => ({}) };
    let prevented = false;
    clickOn(r, ourPin, { preventDefault: () => (prevented = true) });
    expect(prevented).toBe(false);
    expect(r.state.opened).toHaveLength(0);
  });

  test("recording: the click IS the note, with no composer", () => {
    const el = rig({ recording: true });
    clickOn(el, child("BUTTON", "save"));
    expect(el.state.opened).toHaveLength(0);
    expect(el.state.marked[0].anchorId).toBe("save");

    const pt = rig({ recording: true, tool: "point" });
    clickOn(pt, child("BUTTON", "save"));
    expect(pt.state.markedPoints[0]).toEqual({ x: 30, y: 40, nearPath: "#save" });

    const bare = rig({ recording: true });
    clickOn(bare, bare.doc.body);
    expect(bare.state.markedPoints[0]).toEqual({ x: 30, y: 40, nearPath: undefined });
  });
});

/** A pointer event with the two cancellations counted — the whole question for
 *  the swallow below is whether they were called. */
function pointerEvent(target: unknown) {
  const seen = { prevented: 0, stopped: 0 };
  return {
    seen,
    e: {
      target,
      preventDefault() {
        seen.prevented += 1;
      },
      stopPropagation() {
        seen.stopped += 1;
      },
    } as Record<string, unknown>,
  };
}

describe("the outside-click dismissal, from inside the frame (T:8542)", () => {
  test("a mousedown on the app closes an open composer", () => {
    const r = rig({ open: true });
    r.fire("mousedown", pointerEvent(child()).e, true);
    expect(r.state.closes).toBe(1);
  });

  test("a mousedown on our own layer is exempt — the pin's toggle owns it", () => {
    const r = rig({ open: true });
    r.fire("mousedown", pointerEvent({ ...child(), closest: () => ({}) }).e, true);
    expect(r.state.closes).toBe(0);
  });

  test("nothing open, nothing to close", () => {
    const r = rig();
    r.fire("mousedown", pointerEvent(child()).e, true);
    expect(r.state.closes).toBe(0);
  });
});

/**
 * A11's swallow, on the two events a NATIVE FORM CONTROL acts on. The QA round-2
 * bug: clicking the app's `<input type=range>` while placing an Element note
 * both opened the composer and dragged the slider, because the value change is
 * the POINTER's default action and the click was cancelled after the fact.
 */
describe("the pointer's own default action is swallowed too (A11)", () => {
  test("armed: pointerdown on the app is cancelled and stopped", () => {
    const r = rig();
    const p = pointerEvent(child());
    r.fire("pointerdown", p.e, true);
    expect(p.seen).toEqual({ prevented: 1, stopped: 1 });
  });

  test("armed: mousedown too — the compatibility event a browser may fire alone", () => {
    const r = rig();
    const p = pointerEvent(child());
    r.fire("mousedown", p.e, true);
    expect(p.seen).toEqual({ prevented: 1, stopped: 1 });
  });

  test("DISARMED: nothing is swallowed — the reader is USING the app", () => {
    const r = rig({ armed: false });
    const p = pointerEvent(child());
    r.fire("pointerdown", p.e, true);
    r.fire("mousedown", p.e, true);
    expect(p.seen).toEqual({ prevented: 0, stopped: 0 });
  });

  test("our own layer is exempt: the card's textarea still takes focus", () => {
    const r = rig({ open: true });
    const p = pointerEvent({ ...child(), closest: () => ({}) });
    r.fire("pointerdown", p.e, true);
    r.fire("mousedown", p.e, true);
    expect(p.seen).toEqual({ prevented: 0, stopped: 0 });
  });
});
