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
    status: "idle",
    turns: [],
    permissions: [],
    appState: [],
    skills: [],
    working: null,
    trouble: null,
    quota: null,
    permissionMode: "prompt",
    queued: [],
    historyLoading: false,
    adopting: false,
    transcript: null,
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
  // And the correction comes back once the reader returns to the tail: the
  // scroll listener re-arms the flag inside the near-bottom window.
  scroller.scrollTop = scroller.scrollHeight - scroller.clientHeight - 10;
  act(() => scroller.fire("scroll", {}));
  followRef.current?.();
  expect(scroller.scrollTop).toBe(1000);
});

test("the handle is dropped with the scrollport", () => {
  const { followRef } = mount();
  expect(followRef.current).not.toBe(null);
  for (const r of mounted.splice(0)) act(() => r.unmount());
  // A banner effect that fires while no transcript is mounted has nothing to
  // correct, and must not reach a detached node to do it.
  expect(followRef.current).toBe(null);
});
