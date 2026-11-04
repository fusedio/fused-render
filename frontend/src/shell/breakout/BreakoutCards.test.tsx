import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";
import { useEffect } from "react";
import { act, create } from "react-test-renderer";

import { closeAgentPeek, getAgentPeeks, openAgentPeek, publishPeekTurns, resetAgentPeeks } from "@apps/claude/agent-peeks";
import { closeBreakout, getBreakouts, openBreakout, resetBreakouts } from "@platform/lib/breakouts";
import { AgentPeekCard, Card } from "./BreakoutCards";
import { agentPeekTurns } from "@apps/claude/ui/AgentPeekLog";

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

// A mount that reports a fixed breakout state once.
const reporting = (s: unknown) => () => {
  const P = (props: { onBreakoutState?: (s: never) => void }) => {
    useEffect(() => {
      props.onBreakoutState?.(s as never);
    }, [props]);
    return null;
  };
  return P as never;
};
const closeBtn = (r: ReturnType<typeof create>) =>
  r.root.findAllByType("button").find((b) => (b.children as string[]).join("") === "Close")!;

test("close confirms when the card has sends, even after a refresh swapped the u: keys", () => {
  resetBreakouts();
  const id = openBreakout({ sessionId: "s1", file: "/w/proj" });
  const refreshed = {
    sessionId: "brk",
    sends: 1,
    turns: [
      { role: "user", key: "uuid-1", text: "q" },
      { role: "assistant", key: "uuid-2", text: "a" },
    ],
  };
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(<Card card={getBreakouts()[0]!} index={0} mount={reporting(refreshed)()} />, { createNodeMock: () => el });
  });
  const w = window as unknown as Record<string, unknown>;
  const prev = w["confirm"];
  const asked: string[] = [];
  w["confirm"] = (m: string) => (asked.push(m), false);
  try {
    act(() => closeBtn(r).props.onClick());
    expect(asked).toHaveLength(1);
    expect(getBreakouts()).toHaveLength(1);
    w["confirm"] = () => true;
    act(() => closeBtn(r).props.onClick());
    expect(getBreakouts()).toHaveLength(0);
  } finally {
    w["confirm"] = prev;
    closeBreakout(id);
    act(() => r.unmount());
  }
});

test("a failed Send back keeps the card open and shows one error line", async () => {
  resetBreakouts();
  openBreakout({ sessionId: "s1", file: "/w/proj" });
  const state = {
    sessionId: "brk",
    sends: 1,
    turns: [
      { role: "user", key: "uuid-1", text: "q" },
      { role: "assistant", key: "uuid-2", text: "an answer" },
    ],
  };
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(<Card card={getBreakouts()[0]!} index={0} mount={reporting(state)()} />, { createNodeMock: () => el });
  });
  const w = window as unknown as Record<string, unknown>;
  const g = globalThis as unknown as Record<string, unknown>;
  const prevDispatch = w["dispatchEvent"];
  const prevFetch = g["fetch"];
  w["dispatchEvent"] = () => true; // nobody handled it: no parent mounted
  g["fetch"] = () => Promise.reject(new Error("offline")); // the draft read fails
  try {
    const btn = r.root.findAllByType("button").find((b) => (b.children as string[]).join("") === "Send back")!;
    await act(async () => {
      btn.props.onClick();
      await new Promise((res) => setTimeout(res, 20));
    });
    expect(getBreakouts()).toHaveLength(1);
    expect(r.root.findAllByProps({ role: "alert" }).length).toBeGreaterThan(0);
  } finally {
    w["dispatchEvent"] = prevDispatch;
    g["fetch"] = prevFetch;
    act(() => r.unmount());
    resetBreakouts();
  }
});

test("closing a used card mid-run stops the run once the discard is confirmed", () => {
  resetBreakouts();
  openBreakout({ sessionId: "s1", file: "/w/proj" });
  let stops = 0;
  const state = {
    sessionId: "brk",
    sends: 1,
    running: true,
    stop: () => void stops++,
    turns: [{ role: "user", key: "u:1", text: "q" }],
  };
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(<Card card={getBreakouts()[0]!} index={0} mount={reporting(state)()} />, { createNodeMock: () => el });
  });
  const w = window as unknown as Record<string, unknown>;
  const prev = w["confirm"];
  try {
    w["confirm"] = () => false;
    act(() => closeBtn(r).props.onClick());
    expect(stops).toBe(0); // declined: the run goes on
    expect(getBreakouts()).toHaveLength(1);
    w["confirm"] = () => true;
    act(() => closeBtn(r).props.onClick());
    expect(stops).toBe(1);
    expect(getBreakouts()).toHaveLength(0);
  } finally {
    w["confirm"] = prev;
    act(() => r.unmount());
  }
});

test("close does not confirm for an unused card", () => {
  resetBreakouts();
  openBreakout({ sessionId: "s1", file: "/w/proj" });
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(
      <Card card={getBreakouts()[0]!} index={0} mount={reporting({ sessionId: "", sends: 0, turns: [{ role: "user", key: "h-1", text: "parent" }] })()} />,
      { createNodeMock: () => el },
    );
  });
  const w = window as unknown as Record<string, unknown>;
  const prev = w["confirm"];
  w["confirm"] = () => {
    throw new Error("must not confirm");
  };
  try {
    act(() => closeBtn(r).props.onClick());
    expect(getBreakouts()).toHaveLength(0);
  } finally {
    w["confirm"] = prev;
    act(() => r.unmount());
  }
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

test("the default slot never steps a card off the top of a short window", () => {
  const w = window as unknown as Record<string, number>;
  const prev = w["innerHeight"];
  w["innerHeight"] = 300;
  try {
    let r!: ReturnType<typeof create>;
    act(() => {
      r = create(<Card card={card} index={5} mount={NoChat} />, { createNodeMock: () => el });
    });
    const s = r.root.findByProps({ role: "dialog" }).props.style;
    // card height = min(PEEK_MAX_HEIGHT, 0.7 * 300) = 210, so bottom <= 300 - 210.
    expect(s.bottom).toBe(16 + (300 - 16 - 210));
    expect(300 - s.bottom - 210).toBeGreaterThanOrEqual(0);
    act(() => r.unmount());
  } finally {
    w["innerHeight"] = prev;
  }
  const roomy = mount();
  expect(roomy.cardDiv().props.style.bottom).toBe(16);
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

function mountPeek(seam: (seg: unknown) => null) {
  const id = openAgentPeek(peekReq);
  const peek = getAgentPeeks().find((p) => p.id === id)!;
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(<AgentPeekCard peek={peek} index={0} renderLog={seam as never} />, { createNodeMock: () => el });
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

test("the agent peek hands the live Agent segment to the log", () => {
  const seen: { status: string; children: unknown }[] = [];
  const kids = [{ kind: "text", text: "inner" }];
  const { r } = mountPeek((seg) => (seen.push(seg as never), null));
  act(() => publishPeekTurns("s1", turnWith("running", kids)));
  expect(seen[seen.length - 1]).toMatchObject({ status: "running", children: kids });
  act(() => publishPeekTurns("s1", turnWith("ok", kids)));
  expect(seen[seen.length - 1]).toMatchObject({ status: "ok", children: kids });
  expect(r.root.findAllByProps({ className: "agent-peek-empty" }).length).toBe(0);
});

test("a subagent reads as its prompt, then its rows as a reply — live while running", () => {
  const kids = [{ kind: "text", text: "inner" }];
  const seg = { kind: "tool", id: "tu1", name: "Agent", status: "running", input: { prompt: " Find it " }, children: kids };
  expect(agentPeekTurns(seg as never)).toEqual<unknown[]>([
    { role: "user", key: "agent-prompt", text: "Find it" },
    { role: "assistant", key: "agent-reply", text: "", segments: kids, streaming: true },
  ]);
  const orphan = { ...seg, status: "ok", input: {}, orphan: true };
  expect(agentPeekTurns(orphan as never)).toEqual<unknown[]>([{ role: "assistant", key: "agent-reply", text: "", segments: kids }]);
});

test("closing removes the agent peek", () => {
  const { r, peek } = mountPeek(() => null);
  const close = r.root.findAllByType("button").find((b) => allText(b as never) === "Close")!;
  act(() => close.props.onClick());
  expect(getAgentPeeks().some((p) => p.id === peek.id)).toBe(false);
  closeAgentPeek(peek.id);
});
