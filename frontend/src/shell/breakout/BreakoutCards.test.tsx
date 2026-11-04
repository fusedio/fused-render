import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";
import { useEffect } from "react";
import { act, create } from "react-test-renderer";

import { closeAgentPeek, getAgentPeeks, openAgentPeek, publishPeekTurns, resetAgentPeeks } from "@platform/lib/agent-peeks";
import { AgentPeekCard, Card } from "./BreakoutCards";

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

// ---- Agent peek ------------------------------------------------------------
const peekRoots: ReturnType<typeof create>[] = [];
afterEach(() => {
  for (const r of peekRoots.splice(0)) act(() => r.unmount());
  resetAgentPeeks();
});

const peekReq = { sessionId: "s1", file: "/w", toolUseId: "tu1", title: "Explore repo", subtitle: "Explore · opus · running" };
const turnWith = (status: string, children?: unknown[]) =>
  [{ role: "assistant", key: "a", text: "", segments: [{ kind: "tool", id: "tu1", name: "Agent", status, ...(children ? { children } : {}) }] }] as never;

function mountPeek(seam: (segs: unknown[], live: boolean) => null) {
  const id = openAgentPeek(peekReq);
  const peek = getAgentPeeks().find((p) => p.id === id)!;
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(<AgentPeekCard peek={peek} index={0} renderSegments={seam as never} />, { createNodeMock: () => el });
  });
  peekRoots.push(r);
  return { r, peek };
}
const allText = (n: { children: unknown[] }): string =>
  n.children.map((c) => (typeof c === "string" ? c : allText(c as never))).join("");

test("the agent peek shows its title and subtitle, labelled Agent peek", () => {
  const { r } = mountPeek(() => null);
  expect(r.root.findByProps({ role: "dialog" }).props["aria-label"]).toBe("Agent peek");
  expect(allText(r.root.findByProps({ className: "breakout-title" }) as never)).toBe("Explore repo");
  expect(allText(r.root.findByProps({ className: "breakout-note" }) as never)).toBe("Explore · opus · running");
  expect(allText(r.root.findByProps({ className: "agent-peek-empty" }) as never)).toBe("No transcript yet.");
});

test("the agent peek hands the segment's children to the renderer, live while running", () => {
  const seen: { segs: unknown[]; live: boolean }[] = [];
  const kids = [{ kind: "text", text: "inner" }];
  const { r } = mountPeek((segs, live) => (seen.push({ segs, live }), null));
  act(() => publishPeekTurns("s1", turnWith("running", kids)));
  expect(seen[seen.length - 1]).toEqual({ segs: kids, live: true });
  act(() => publishPeekTurns("s1", turnWith("ok", kids)));
  expect(seen[seen.length - 1]).toEqual({ segs: kids, live: false });
  expect(r.root.findAllByProps({ className: "agent-peek-empty" }).length).toBe(0);
});

test("closing removes the agent peek", () => {
  const { r, peek } = mountPeek(() => null);
  const close = r.root.findAllByType("button").find((b) => allText(b as never) === "Close")!;
  act(() => close.props.onClick());
  expect(getAgentPeeks().some((p) => p.id === peek.id)).toBe(false);
  closeAgentPeek(peek.id);
});
