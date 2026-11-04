import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterAll, beforeAll, expect, test } from "bun:test";
import { useEffect } from "react";
import { act, create } from "react-test-renderer";

import { Card } from "./BreakoutCards";

// Only the chat is stubbed (via the Card `mount` prop); the card chrome and drag logic are the real ones.
const NoChat = () => null;

// Stub per-suite and restore: bun shares one globalThis across files, so a permanent overwrite
// would reroute every later suite's addEventListener onto this private bus.
const bus = new EventTarget();
const winStubs: Record<string, unknown> = {
  addEventListener: bus.addEventListener.bind(bus),
  removeEventListener: bus.removeEventListener.bind(bus),
  innerWidth: 2000,
  innerHeight: 1500,
};
const globalStubs: Record<string, unknown> = {
  requestAnimationFrame: (fn: () => void) => (fn(), 1),
  cancelAnimationFrame: () => {},
};
const targets: [Record<string, unknown>, Record<string, unknown>][] = [
  [window as unknown as Record<string, unknown>, winStubs],
  [globalThis as unknown as Record<string, unknown>, globalStubs],
];
const saved: { obj: Record<string, unknown>; k: string; had: boolean; value: unknown }[] = [];
beforeAll(() => {
  for (const [obj, stubs] of targets)
    for (const [k, v] of Object.entries(stubs)) {
      saved.push({ obj, k, had: k in obj, value: obj[k] });
      obj[k] = v;
    }
});
afterAll(() => {
  for (const { obj, k, had, value } of saved.reverse()) {
    if (had) obj[k] = value;
    else delete obj[k];
  }
  saved.length = 0;
});

const card = { id: "b1", parentSessionId: "s1", file: "/w/proj" } as never;
const rect = { left: 500, top: 300, width: 400, height: 500 };
const el = { getBoundingClientRect: () => rect };

function mount() {
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(<Card card={card} index={0} mount={NoChat} />, { createNodeMock: () => el });
  });
  const root = r.root;
  return {
    r,
    cardDiv: () => root.findByProps({ role: "dialog" }),
    head: () => root.findByProps({ className: "claude-rail-peek-head breakout-head" }),
    headDragging: () => root.findByProps({ className: "claude-rail-peek-head breakout-head is-dragging" }),
  };
}
const ev = (x: number, y: number, target: unknown = {}) => ({
  clientX: x,
  clientY: y,
  pointerId: 1,
  target,
  currentTarget: { setPointerCapture() {}, releasePointerCapture() {} },
});

test("the note says when the fork was clamped to the last compaction", () => {
  const Clamped = (props: { onBreakoutState?: (s: { sessionId: string; turns: []; forkClamped?: boolean }) => void }) => {
    useEffect(() => {
      props.onBreakoutState?.({ sessionId: "s", turns: [], forkClamped: true });
    }, [props]);
    return null;
  };
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(<Card card={card} index={0} mount={Clamped as never} />, { createNodeMock: () => el });
  });
  const note = r.root.findByProps({ className: "breakout-note" });
  expect(note.children.join("")).toContain("forked at the last compaction");
  expect(note.props.title).toContain("last compaction");
  const plain = mount().r.root.findByProps({ className: "breakout-note" });
  expect(plain.children.join("")).toBe("research only");
});

test("dragging the head switches the card from right/bottom to left/top", () => {
  const m = mount();
  expect(m.cardDiv().props.style.right).toBe(16);
  expect(m.cardDiv().props.style.left).toBeUndefined();
  const h = m.head().props;
  act(() => h.onPointerDown(ev(100, 100)));
  act(() => m.headDragging().props.onPointerMove(ev(160, 140)));
  act(() => m.headDragging().props.onPointerUp(ev(160, 140)));
  const s = m.cardDiv().props.style;
  expect(s.left).toBe(560);
  expect(s.top).toBe(340);
  expect(s.right).toBeUndefined();
  expect(s.bottom).toBeUndefined();
});

test("the card is clamped to the screen", () => {
  const m = mount();
  act(() => m.head().props.onPointerDown(ev(100, 100)));
  act(() => m.headDragging().props.onPointerMove(ev(-5000, 9000)));
  act(() => m.headDragging().props.onPointerUp(ev(-5000, 9000)));
  const s = m.cardDiv().props.style;
  expect(s.left).toBe(0);
  expect(s.top).toBe(1000);
});

test("a pointerdown on a button does not start a drag", () => {
  const m = mount();
  const button = { closest: (sel: string) => (sel === "button" ? {} : null) };
  act(() => m.head().props.onPointerDown(ev(100, 100, button)));
  act(() => m.head().props.onPointerMove(ev(160, 140)));
  const s = m.cardDiv().props.style;
  expect(s.right).toBe(16);
  expect(s.left).toBeUndefined();
});
