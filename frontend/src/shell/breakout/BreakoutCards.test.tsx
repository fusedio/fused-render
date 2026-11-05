import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";
import { useEffect } from "react";
import { act, create } from "react-test-renderer";

import { closeAgentPeek, getAgentPeeks, openAgentPeek, publishPeekTurns, resetAgentPeeks } from "@apps/claude/agent-peeks";
import { closeBreakout, consumeBreakoutPrompt, getBreakouts, openBreakout, resetBreakouts } from "@platform/lib/breakouts";
import { AgentPeekCard, Card, HANDOFF_PROMPT } from "./BreakoutCards";
import AgentPeekLog, { agentPeekTurns, agentPeekWork } from "@apps/claude/ui/AgentPeekLog";
import { RunTrigger } from "@apps/claude/ui/RunTrigger";

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

test("the chat mounts fresh: no parent session, the selected messages as context", () => {
  let seen: Record<string, unknown> = {};
  const Spy = (props: Record<string, unknown>) => {
    seen = props;
    return null;
  };
  const withContext = {
    ...(card as object),
    context: { block: "<breakout-context>\nx\n</breakout-context>", count: 2, title: "Parent" },
  } as never;
  act(() => {
    create(<Card card={withContext} index={0} mount={Spy as never} />, { createNodeMock: () => el });
  });
  expect(seen["sessionId"]).toBeUndefined();
  expect(seen["breakout"]).toBe(true);
  // A breakout is Claude-only: it must not inherit a non-Claude catalog default.
  expect(seen["backend"]).toBe("claude");
  expect(seen["breakoutContext"]).toEqual({ block: "<breakout-context>\nx\n</breakout-context>", count: 2, title: "Parent" });
  expect("breakoutAt" in seen).toBe(false);
});

test("the note reads research only", () => {
  expect(mount().r.root.findByProps({ className: "breakout-note" }).children.join("")).toBe("research only");
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

test("close confirms once the card has a user turn", () => {
  resetBreakouts();
  const id = openBreakout({ sessionId: "s1", file: "/w/proj", context: { block: "b", count: 1 } });
  const refreshed = {
    sessionId: "brk",
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

// A mount whose reported state the test drives, with `send` and `stop` spies.
function driven() {
  const calls = { sent: [] as string[], stops: 0 };
  let push!: (s: Record<string, unknown>) => void;
  const P = (props: { onBreakoutState?: (s: never) => void }) => {
    useEffect(() => {
      push = (s) =>
        props.onBreakoutState?.({ sessionId: "brk", send: (t: string) => void calls.sent.push(t), stop: () => void calls.stops++, ...s } as never);
    }, [props]);
    return null;
  };
  return { calls, Mount: P as never, report: (s: Record<string, unknown>) => act(() => push(s)) };
}
const U1 = { role: "user", key: "u1", text: "q" };
const A1 = { role: "assistant", key: "a1", text: "an earlier answer" };
const PU = { role: "user", key: "u2", text: HANDOFF_PROMPT };
const labelOf = (b: { children: unknown }) => (b.children as string[]).join("");
const btnNamed = (r: ReturnType<typeof create>, name: string) => r.root.findAllByType("button").find((b) => labelOf(b) === name);

async function setup() {
  resetBreakouts();
  openBreakout({ sessionId: "s1", file: "/w/proj", context: { block: "b", count: 1 } });
  const d = driven();
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(<Card card={getBreakouts()[0]!} index={0} mount={d.Mount} />, { createNodeMock: () => el });
  });
  d.report({ turns: [U1, A1], running: false });
  // The composer event is the parent's seat: a handled event means "draft filed".
  const drafts: string[] = [];
  const w = window as unknown as Record<string, unknown>;
  const prevDispatch = w["dispatchEvent"];
  w["dispatchEvent"] = (e: { detail?: { text?: string } }) => {
    drafts.push(e.detail?.text ?? "");
    return false;
  };
  const done = () => {
    w["dispatchEvent"] = prevDispatch;
    act(() => r.unmount());
    resetBreakouts();
  };
  return { r, d, drafts, done };
}

test("Send back sends the fixed prompt, files nothing until the run completes, then drafts the NEW reply", async () => {
  const { r, d, drafts, done } = await setup();
  try {
    act(() => btnNamed(r, "Send back")!.props.onClick());
    expect(d.calls.sent).toEqual([HANDOFF_PROMPT]);
    expect(btnNamed(r, "Writing handoff…")!.props.disabled).toBe(true);
    expect(btnNamed(r, "Cancel")).toBeDefined();
    // Running: still nothing filed, the card stays.
    d.report({ turns: [U1, A1, PU, { role: "assistant", key: "a2", text: "partial", streaming: true }], running: true });
    expect(drafts).toHaveLength(0);
    expect(getBreakouts()).toHaveLength(1);
    // Done: the draft is the handoff reply, not the earlier one, and the card closes.
    await act(async () => {
      d.report({ turns: [U1, A1, PU, { role: "assistant", key: "a2", text: "The handoff note." }], running: false });
      await new Promise((res) => setTimeout(res, 20));
    });
    expect(drafts).toEqual(["The handoff note."]);
    expect(getBreakouts()).toHaveLength(0);
  } finally {
    done();
  }
});

test("the draft carries the Handoff file line when the handoff run wrote a .md", async () => {
  const { r, d, drafts, done } = await setup();
  try {
    act(() => btnNamed(r, "Send back")!.props.onClick());
    const reply = {
      role: "assistant",
      key: "a2",
      text: "Note.",
      segments: [{ kind: "tool", id: "t", name: "Write", status: "ok", input: { file_path: "/w/.claude/handoffs/h.md" } }],
    };
    await act(async () => {
      d.report({ turns: [U1, A1, PU], running: true });
      d.report({ turns: [U1, A1, PU, reply], running: false });
      await new Promise((res) => setTimeout(res, 20));
    });
    expect(drafts[0]).toBe("Note.\n\nHandoff file: /w/.claude/handoffs/h.md");
  } finally {
    done();
  }
});

test("an empty reply keeps the card open with the inline error and restores the button", async () => {
  const { r, d, drafts, done } = await setup();
  try {
    act(() => btnNamed(r, "Send back")!.props.onClick());
    await act(async () => {
      d.report({ turns: [U1, A1, PU], running: true });
      d.report({ turns: [U1, A1, PU, { role: "assistant", key: "a2", text: "  " }], running: false });
      await new Promise((res) => setTimeout(res, 20));
    });
    expect(drafts).toHaveLength(0);
    expect(getBreakouts()).toHaveLength(1);
    const alert = r.root.findAllByProps({ role: "alert" });
    expect(alert.length).toBeGreaterThan(0);
    expect(JSON.stringify(alert[0]!.children)).toContain("Couldn't write a handoff. Try again");
    expect(btnNamed(r, "Send back")!.props.disabled).toBe(false);
  } finally {
    done();
  }
});

test("a run that errors with no reply after the prompt keeps the card open with the error", async () => {
  const { r, d, drafts, done } = await setup();
  try {
    act(() => btnNamed(r, "Send back")!.props.onClick());
    await act(async () => {
      d.report({ turns: [U1, A1, PU], running: true });
      // The earlier reply must not be mistaken for the handoff.
      d.report({ turns: [U1, A1, PU], running: false });
      await new Promise((res) => setTimeout(res, 20));
    });
    expect(drafts).toHaveLength(0);
    expect(getBreakouts()).toHaveLength(1);
    expect(r.root.findAllByProps({ role: "alert" }).length).toBeGreaterThan(0);
  } finally {
    done();
  }
});

test("Cancel stops the run, restores the button, and a late finish files nothing", async () => {
  const { r, d, drafts, done } = await setup();
  try {
    act(() => btnNamed(r, "Send back")!.props.onClick());
    d.report({ turns: [U1, A1, PU], running: true });
    act(() => btnNamed(r, "Cancel")!.props.onClick());
    expect(d.calls.stops).toBe(1);
    expect(btnNamed(r, "Cancel")).toBeUndefined();
    d.report({ turns: [U1, A1, PU, { role: "assistant", key: "a2", text: "late" }], running: false });
    expect(drafts).toHaveLength(0);
    expect(btnNamed(r, "Send back")!.props.disabled).toBe(false);
  } finally {
    done();
  }
});

test("Send back is disabled with a hint while the breakout runs, and before any reply; Close still works", async () => {
  const { r, d, done } = await setup();
  try {
    d.report({ turns: [U1, A1, { role: "user", key: "u3", text: "more" }], running: true });
    const b = btnNamed(r, "Send back")!;
    expect(b.props.disabled).toBe(true);
    expect(b.props["data-hint"]).toBe("Wait for the breakout to finish");
    act(() => b.props.onClick());
    expect(d.calls.sent).toHaveLength(0);
    d.report({ turns: [U1], running: false });
    expect(btnNamed(r, "Send back")!.props.disabled).toBe(true);
    // Close mid-writing still closes (and stops the run).
    d.report({ turns: [U1, A1], running: false });
    act(() => btnNamed(r, "Send back")!.props.onClick());
    d.report({ turns: [U1, A1, PU], running: true });
    const w = window as unknown as Record<string, unknown>;
    const prev = w["confirm"];
    w["confirm"] = () => true;
    try {
      act(() => closeBtn(r).props.onClick());
    } finally {
      w["confirm"] = prev;
    }
    expect(d.calls.stops).toBe(1);
    expect(getBreakouts()).toHaveLength(0);
  } finally {
    done();
  }
});

test("a draft that cannot be filed keeps the card open and shows one error line", async () => {
  const { r, d, done } = await setup();
  const w = window as unknown as Record<string, unknown>;
  const g = globalThis as unknown as Record<string, unknown>;
  const prevFetch = g["fetch"];
  w["dispatchEvent"] = () => true; // nobody handled it: no parent mounted
  g["fetch"] = () => Promise.reject(new Error("offline")); // the draft read fails
  try {
    act(() => btnNamed(r, "Send back")!.props.onClick());
    await act(async () => {
      d.report({ turns: [U1, A1, PU], running: true });
      d.report({ turns: [U1, A1, PU, { role: "assistant", key: "a2", text: "note" }], running: false });
      await new Promise((res) => setTimeout(res, 20));
    });
    expect(getBreakouts()).toHaveLength(1);
    expect(r.root.findAllByProps({ role: "alert" }).length).toBeGreaterThan(0);
  } finally {
    g["fetch"] = prevFetch;
    done();
  }
});

test("closing a used card mid-run stops the run once the discard is confirmed", () => {
  resetBreakouts();
  openBreakout({ sessionId: "s1", file: "/w/proj", context: { block: "b", count: 1 } });
  let stops = 0;
  const state = {
    sessionId: "brk",
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
  openBreakout({ sessionId: "s1", file: "/w/proj", context: { block: "b", count: 1 } });
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(
      <Card card={getBreakouts()[0]!} index={0} mount={reporting({ sessionId: "", turns: [] })()} />,
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

test("the agent peek head is a short label, the description as its note, the facts on hover", () => {
  const { r } = mountPeek(() => null);
  expect(r.root.findByProps({ role: "dialog" }).props["aria-label"]).toBe("Agent peek");
  expect(allText(r.root.findByProps({ className: "breakout-title" }) as never)).toBe("Agent");
  const note = r.root.findByProps({ className: "breakout-note" });
  expect(allText(note as never)).toBe("Explore repo");
  expect(note.props["data-hint"]).toBe("Explore repo — Explore · opus · running");
  expect(note.props.title).toBeUndefined();
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

const tool = (status: string) => ({ kind: "tool", id: "t", name: "Read", status });
const text = (t: string) => ({ kind: "text", text: t });

test("a subagent's reply keeps the closing text; the prose and calls before it fold into the work", () => {
  const kids = [text("Let me look."), tool("ok"), text("Now the config."), tool("ok"), text("Found it."), text("In a.ts.")];
  const seg = { kind: "tool", id: "tu1", name: "Agent", status: "ok", input: { prompt: "Find it" }, children: kids };
  expect(agentPeekWork(seg as never)).toEqual<unknown>(kids.slice(0, 4));
  const [, reply] = agentPeekTurns(seg as never);
  expect((reply as { segments: unknown[] }).segments).toEqual<unknown>(kids.slice(4));
});

test("a subagent still on a call keeps that call visible with the text after it", () => {
  const kids = [text("Let me look."), tool("ok"), text("Running it."), tool("running")];
  const seg = { kind: "tool", id: "tu1", name: "Agent", status: "running", children: kids };
  expect(agentPeekWork(seg as never)).toEqual<unknown>(kids.slice(0, 3));
  const [reply] = agentPeekTurns(seg as never);
  expect((reply as { segments: unknown[] }).segments).toEqual<unknown>(kids.slice(3));
});

test("a parallel batch of running calls all stay in the answer", () => {
  const kids = [text("Reading."), tool("ok"), text("Both at once."), tool("running"), tool("running"), tool("ok")];
  const seg = { kind: "tool", id: "tu1", name: "Agent", status: "running", children: kids };
  expect(agentPeekWork(seg as never)).toEqual<unknown>(kids.slice(0, 3));
  const [reply] = agentPeekTurns(seg as never);
  expect((reply as { segments: unknown[] }).segments).toEqual<unknown>(kids.slice(3));
});

test("a subagent with no calls folds nothing; one that is only calls folds them all", () => {
  const prose = [text("a"), text("b")];
  expect(agentPeekWork({ kind: "tool", status: "ok", children: prose } as never)).toEqual<unknown>([]);
  const calls = [tool("ok"), tool("ok")];
  const seg = { kind: "tool", status: "ok", children: calls };
  expect(agentPeekWork(seg as never)).toEqual<unknown>(calls);
  const [reply] = agentPeekTurns(seg as never);
  expect((reply as { segments: unknown[] }).segments).toEqual([]);
});

test("opening the work or prompt fold releases the peek's tail-follow", () => {
  const long = "word ".repeat(200);
  const kids = [text("Looking."), tool("ok"), text("Done.")];
  const mk = (n: number) =>
    ({ kind: "tool", id: "tu1", name: "Agent", status: "running", input: { prompt: long }, children: [...kids, ...Array(n).fill(text("more"))] }) as never;
  const port = { scrollTop: 0, scrollHeight: 1000, clientHeight: 100 };
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(<AgentPeekLog seg={mk(0)} />, { createNodeMock: (el) => (el.props.className === "chat-logwrap" ? port : null) });
  });
  expect(port.scrollTop).toBe(1000);
  const open = r.root.findAllByType(RunTrigger)[0]!;
  act(() => open.props.onToggle());
  port.scrollTop = 0;
  act(() => r.update(<AgentPeekLog seg={mk(1)} />));
  expect(port.scrollTop).toBe(0);
});

test("closing removes the agent peek", () => {
  const { r, peek } = mountPeek(() => null);
  const close = r.root.findAllByType("button").find((b) => allText(b as never) === "Close")!;
  act(() => close.props.onClick());
  expect(getAgentPeeks().some((p) => p.id === peek.id)).toBe(false);
  closeAgentPeek(peek.id);
});

test("the card hands its prompt to the chat once; consuming it forgets it, and the dedupe path never re-adds it", () => {
  resetBreakouts();
  const ctx = { block: "b", count: 1 };
  const req = { sessionId: "s1", file: "/w/p", context: ctx, forkKey: "a|b", prompt: "why?" };
  const id = openBreakout(req);
  let seen: Record<string, unknown> = {};
  const Spy = (props: Record<string, unknown>) => {
    seen = props;
    return null;
  };
  act(() => {
    create(<Card card={getBreakouts()[0]!} index={0} mount={Spy as never} />, { createNodeMock: () => el });
  });
  expect(seen["breakoutPrompt"]).toBe("why?");
  (seen["onBreakoutPromptSent"] as () => void)();
  expect(getBreakouts()[0]!.prompt).toBeUndefined();
  expect(openBreakout({ ...req, prompt: "again" })).toBe(id);
  expect(getBreakouts()[0]!.prompt).toBeUndefined();
  consumeBreakoutPrompt(id);
  resetBreakouts();
});
