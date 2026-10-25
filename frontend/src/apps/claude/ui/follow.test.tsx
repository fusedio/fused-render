// THE FOLLOW FLAG, AS LENT OUT (T:12758, T:17211-17213).
//
// The schedule banner has to correct a bottom-pinned transcript when it appears
// — it SHRINKS the scrollport, so the last line moves by the banner's height —
// and the port's `followBottom` used to do that by writing `scrollTop =
// scrollHeight` off a raw `.chat-logwrap` lookup. That jumped a reader who had
// scrolled up to the latest turn the moment a pending message landed (Bugbot,
// PR #1075).
//
// T calls `followBottom()` there, which is the FLAG's, not `scrollBottom()`.
// These pin that the handle the scrollport lends out is the flag's too — and
// that it cannot be a geometry read taken by the caller instead, because the
// banner moves the viewport and not the reader.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { act, create } from "react-test-renderer";
import { createRef } from "react";

import type { ChatState } from "../protocol/controller-api";
import { Transcript } from "./Transcript";

const mounted: Array<ReturnType<typeof create>> = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
});

/** Enough of a scrollport for the follow rules: they read the three geometry
 *  numbers and write `scrollTop` back, and nothing else. */
function fakeScroller() {
  const listeners: Record<string, Array<(e: unknown) => void>> = {};
  return {
    scrollTop: 0,
    scrollHeight: 1000,
    clientHeight: 300,
    // PR3's receipt-visibility pass samples the port's box on every commit
    // (`Transcript`'s permissions effect), so a stub scrollport has to answer
    // geometry as well as the three scroll numbers.
    getBoundingClientRect: () => ({ top: 0, bottom: 300, left: 0, right: 800, width: 800, height: 300 }),
    addEventListener(type: string, fn: (e: unknown) => void) {
      (listeners[type] ||= []).push(fn);
    },
    removeEventListener(type: string, fn: (e: unknown) => void) {
      listeners[type] = (listeners[type] || []).filter((f) => f !== fn);
    },
    fire(type: string, e: unknown) {
      for (const fn of [...(listeners[type] || [])]) fn(e);
    },
  };
}

function state(over: Partial<ChatState> = {}): ChatState {
  return {
    file: "/proj",
    sessionId: "s1",
    runId: null,
    lastRunId: null,
    status: "idle",
    turns: [],
    permissions: [],
    appState: [],
    canvasShot: [],
    skills: [],
    working: null,
    trouble: null,
    permissionMode: "prompt",
    queued: [],
    inbox: [],
    historyLoading: false,
    adopting: false,
    transcript: null,
    context: null,
    tasks: [],
    tasksRunId: null,
    ownRunEndedAt: 0,
    repaired: 0,
    transcriptGen: 0,
    rev: 1,
    ...over,
  };
}

const actions = {
  decidePermission: async () => {},
  answerQuestion: async () => {},
  decidePlan: async () => {},
  dismissCard: () => {},
  stopRun: async () => {},
};

/** Mounts a transcript over a fake scrollport and hands back both it and the
 *  lent handle. */
function mount() {
  const scroller = fakeScroller();
  const followRef = createRef<(() => void) | null>() as {
    current: (() => void) | null;
  };
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(<Transcript state={state()} actions={actions} followRef={followRef} />, {
      createNodeMock: (el) => {
        const cls = String((el.props as { className?: string }).className ?? "");
        return cls.includes("chat-logwrap") ? scroller : {};
      },
    });
  });
  mounted.push(r);
  return { scroller, followRef };
}

test("the scrollport lends the flag-guarded followBottom, not a raw scroll", () => {
  const { scroller, followRef } = mount();
  expect(typeof followRef.current).toBe("function");
  // A READER AT THE TAIL. The flag starts armed, so the banner's correction is
  // exactly the re-pin it is there to do.
  scroller.scrollTop = 0;
  followRef.current?.();
  expect(scroller.scrollTop).toBe(1000);
});

test("A READER WHO HAS SCROLLED UP IS NOT JUMPED when the banner appears", () => {
  const { scroller, followRef } = mount();
  // A wheel flick upward drops the follow with no distance threshold — the one
  // gesture the old raw write could not see.
  scroller.scrollTop = 200;
  act(() => scroller.fire("wheel", { deltaY: -50 }));
  followRef.current?.();
  expect(scroller.scrollTop).toBe(200);
  // And the correction comes back once the reader returns to the tail: a
  // wheel DOWN and the scroll it produces re-arm the flag inside the
  // near-bottom window.
  scroller.scrollTop = scroller.scrollHeight - scroller.clientHeight - 10;
  act(() => scroller.fire("wheel", { deltaY: 50 }));
  act(() => scroller.fire("scroll", {}));
  followRef.current?.();
  expect(scroller.scrollTop).toBe(1000);
});

test("A SCROLL EVENT WITH NO GESTURE BEHIND IT MOVES THE FLAG NEITHER WAY (Akshil 2026-10-19)", () => {
  const { scroller, followRef } = mount();
  // WebKit (the desktop app): the first streamed line overflows the port by a
  // fraction of a pixel, our write lands on 2, the engine snaps it to 0 a
  // frame later. That 2px "move up" used to turn the follow off for the rest
  // of the run.
  scroller.scrollHeight = 302;
  scroller.scrollTop = 2;
  act(() => scroller.fire("scroll", {}));
  scroller.scrollTop = 0;
  act(() => scroller.fire("scroll", {}));
  scroller.scrollHeight = 1000;
  followRef.current?.();
  expect(scroller.scrollTop).toBe(1000);
  // …and the mirror image: a reader who left the tail is not re-armed by a
  // clamp that happens to land them inside the near-bottom window. The wheel's
  // own window has to be OVER for this to prove anything, so the clock is
  // moved past it.
  act(() => scroller.fire("wheel", { deltaY: -50 }));
  scroller.scrollTop = 200;
  act(() => scroller.fire("scroll", {}));
  const realNow = performance.now.bind(performance);
  performance.now = () => realNow() + 60_000;
  try {
    scroller.scrollHeight = 520;
    scroller.scrollTop = 200;
    act(() => scroller.fire("scroll", {}));
    followRef.current?.();
    expect(scroller.scrollTop).toBe(200);
  } finally {
    performance.now = realNow;
  }
});

test("a wheel DOWN that rubber-bands off the bottom edge still re-arms", () => {
  const { scroller, followRef } = mount();
  act(() => scroller.fire("wheel", { deltaY: -50 }));
  scroller.scrollTop = 200;
  act(() => scroller.fire("scroll", {}));
  // Down to the edge: the bounce back lands ON the maximum, a decrease in
  // scrollTop — which is not a move up, because the gesture was down.
  act(() => scroller.fire("wheel", { deltaY: 600 }));
  scroller.scrollTop = 730;
  act(() => scroller.fire("scroll", {}));
  scroller.scrollTop = 700;
  act(() => scroller.fire("scroll", {}));
  scroller.scrollHeight = 1400;
  followRef.current?.();
  expect(scroller.scrollTop).toBe(1400);
});

test("a wheel DOWN while already on the tail re-arms without a scroll event (CodeRabbit, PR #1748)", () => {
  const { scroller, followRef } = mount();
  act(() => scroller.fire("wheel", { deltaY: -50 }));
  // A shrink clamps the reader onto the bottom edge; there is nowhere left to
  // scroll, so the next wheel tick produces no scroll event at all.
  scroller.scrollHeight = 500;
  scroller.scrollTop = 200;
  act(() => scroller.fire("wheel", { deltaY: 50 }));
  scroller.scrollHeight = 900;
  followRef.current?.();
  expect(scroller.scrollTop).toBe(900);
});

test("sending inside a flick's window keeps the follow the send re-armed (CodeRabbit, PR #1748)", () => {
  const scroller = fakeScroller();
  const followRef = createRef<(() => void) | null>() as { current: (() => void) | null };
  const turn = (key: string, role: "user" | "assistant") =>
    ({ key, role, text: "x", segments: [], streaming: false, uuid: key }) as unknown as ChatState["turns"][number];
  let r!: ReturnType<typeof create>;
  const mock = {
    createNodeMock: (el: { props: { className?: string } }) =>
      String(el.props.className ?? "").includes("chat-logwrap") ? scroller : {},
  };
  act(() => {
    r = create(<Transcript state={state({ turns: [turn("u1", "user")] })} actions={actions} followRef={followRef} />, mock);
  });
  mounted.push(r);
  // Flick up, then send straight away: the new-turn effect re-arms and writes
  // to the bottom, and that write's own scroll event lands inside the flick's
  // window.
  scroller.scrollTop = 200;
  act(() => scroller.fire("wheel", { deltaY: -50 }));
  act(() => {
    r.update(<Transcript state={state({ turns: [turn("u1", "user"), turn("u2", "user")] })} actions={actions} followRef={followRef} />);
  });
  act(() => scroller.fire("scroll", {}));
  scroller.scrollHeight = 1400;
  followRef.current?.();
  expect(scroller.scrollTop).toBe(1400);
});

test("a scroll key is a gesture; a key in a field is not", () => {
  const { scroller, followRef } = mount();
  act(() => scroller.fire("keydown", { key: "PageUp", target: { tagName: "DIV", isContentEditable: false } }));
  scroller.scrollTop = 200;
  act(() => scroller.fire("scroll", {}));
  followRef.current?.();
  expect(scroller.scrollTop).toBe(200);
  act(() => scroller.fire("keydown", { key: "End", target: { tagName: "DIV", isContentEditable: false } }));
  scroller.scrollTop = 700;
  act(() => scroller.fire("scroll", {}));
  followRef.current?.();
  expect(scroller.scrollTop).toBe(1000);
  // Arrow keys inside a question card's text field move its caret, not the port.
  act(() => scroller.fire("keydown", { key: "ArrowUp", target: { tagName: "TEXTAREA", isContentEditable: false } }));
  scroller.scrollTop = 500;
  act(() => scroller.fire("scroll", {}));
  followRef.current?.();
  expect(scroller.scrollTop).toBe(1000);
});

test("THE AUTO-FOLD SHRINKING THE LOG DOES NOT RE-ARM THE FOLLOW (Akshil 2026-09-15)", () => {
  const { scroller, followRef } = mount();
  // A reader who has scrolled up to read the middle of the conversation.
  scroller.scrollTop = 200;
  act(() => scroller.fire("wheel", { deltaY: -50 }));
  act(() => scroller.fire("scroll", {}));
  followRef.current?.();
  expect(scroller.scrollTop).toBe(200);
  // Now a new reply lands and the rule folds the previous one: `.chat-log`
  // loses most of its height and the browser CLAMPS `scrollTop` down with it.
  // The reader has not moved — but the tail has moved UP TO MEET THEM, so the
  // near-bottom window they are suddenly inside is the fold's doing and not a
  // gesture. Read as "they are at the bottom", it re-armed the follow and the
  // next write yanked them to the tail.
  scroller.scrollHeight = 400;
  scroller.scrollTop = 100;
  act(() => scroller.fire("scroll", {}));
  followRef.current?.();
  expect(scroller.scrollTop).toBe(100);
});

test("a reader who WAS at the tail is still carried down by the fold", () => {
  const { scroller, followRef } = mount();
  // Nothing turned the flag off, so the shrink changes nothing about it: the
  // guard only stops a shrink from RE-ARMING, it never disarms.
  act(() => scroller.fire("scroll", {}));
  scroller.scrollHeight = 400;
  scroller.scrollTop = 100;
  act(() => scroller.fire("scroll", {}));
  followRef.current?.();
  expect(scroller.scrollTop).toBe(400);
});

test("THE COMPOSER FOLDING AFTER A SEND DOES NOT DROP THE FOLLOW (Akshil 2026-10-16)", () => {
  const { scroller, followRef } = mount();
  // A reader at the tail who has just sent a reply.
  scroller.scrollTop = 700;
  act(() => scroller.fire("scroll", {}));
  // The composer card folds back to one line, so the scrollport GROWS by the
  // rows the card gave up — and the browser clamps `scrollTop` down by the same
  // amount. The reader did not move; the viewport did.
  scroller.clientHeight = 340;
  scroller.scrollTop = 660;
  act(() => scroller.fire("scroll", {}));
  // The reply streams in. A port that still follows carries them down with it.
  scroller.scrollHeight = 1400;
  followRef.current?.();
  expect(scroller.scrollTop).toBe(1400);
});

test("…EVEN WHEN THE REPLY GREW THE LOG IN THE SAME FRAME (CodeRabbit, PR #1703)", () => {
  const { scroller, followRef } = mount();
  scroller.scrollTop = 700;
  act(() => scroller.fire("scroll", {}));
  // The port grows and clamps the reader, and the streaming reply lands more
  // log in the same layout — so when the scroll event runs, the clamped
  // position is already short of the new maximum.
  scroller.clientHeight = 340;
  scroller.scrollHeight = 1400;
  scroller.scrollTop = 660;
  act(() => scroller.fire("scroll", {}));
  followRef.current?.();
  expect(scroller.scrollTop).toBe(1400);
});

test("a drag up the bar after the port grew is still a gesture", () => {
  const { scroller, followRef } = mount();
  scroller.scrollTop = 700;
  act(() => scroller.fire("scroll", {}));
  // The port grows and clamps the reader to the new bottom edge…
  scroller.clientHeight = 340;
  scroller.scrollTop = 660;
  act(() => scroller.fire("scroll", {}));
  // …which left the follow armed (the clamp is a no-op on the flag; the stub
  // port does not clamp the write, so "armed" reads as the full height)…
  followRef.current?.();
  expect(scroller.scrollTop).toBe(1000);
  // …and then they drag the scrollbar up to read: the pointer is down on the
  // port's own box (the bar is not a child), so the scroll is a gesture.
  act(() => scroller.fire("pointerdown", { target: scroller }));
  scroller.scrollTop = 500;
  act(() => scroller.fire("scroll", {}));
  scroller.scrollHeight = 1400;
  followRef.current?.();
  expect(scroller.scrollTop).toBe(500);
  // Dragging it back down to the edge re-arms.
  act(() => scroller.fire("pointerdown", { target: scroller }));
  scroller.scrollTop = 1060;
  act(() => scroller.fire("scroll", {}));
  scroller.scrollHeight = 1600;
  followRef.current?.();
  expect(scroller.scrollTop).toBe(1600);
});

test("the handle is dropped with the scrollport", () => {
  const { followRef } = mount();
  expect(followRef.current).not.toBe(null);
  for (const r of mounted.splice(0)) act(() => r.unmount());
  // A banner effect that fires while no transcript is mounted has nothing to
  // correct, and must not reach a detached node to do it.
  expect(followRef.current).toBe(null);
});
