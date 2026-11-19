// The specialist breakout card (docs/specialists.md): the chat it mounts,
// the close flow that drafts the summary into the task's composer, and Discard.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterAll, afterEach, beforeAll, describe, expect, test } from "bun:test";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";

import {
  getSpecialistBreakouts,
  openSpecialistBreakout,
  resetSpecialistBreakouts,
} from "@platform/lib/specialist-breakouts";
import type { ComposerDraftDetail } from "@platform/lib/breakouts";
import { cardBox, clipTitle, DELETED_POLL_MS, SpecialistBreakoutCard, type SpecialistCardDeps } from "./SpecialistBreakoutCard";

const winStubs: Record<string, unknown> = { innerWidth: 2000, innerHeight: 1500 };
const globalStubs: Record<string, unknown> = {
  requestAnimationFrame: (fn: () => void) => (fn(), 1),
  cancelAnimationFrame: () => {},
};
const saved: { obj: Record<string, unknown>; k: string; had: boolean; value: unknown }[] = [];
beforeAll(() => {
  for (const [obj, stubs] of [
    [window as unknown as Record<string, unknown>, winStubs],
    [globalThis as unknown as Record<string, unknown>, globalStubs],
  ] as const)
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
});
const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  resetSpecialistBreakouts();
});

const textOf = (n: ReactTestInstance | string): string =>
  typeof n === "string" ? n : n.children.map((c) => textOf(c as ReactTestInstance | string)).join("");
const button = (r: ReactTestRenderer, label: string) => r.root.find((n) => n.type === "button" && textOf(n).trim() === label);

type Report = { sessionId: string; turns: readonly unknown[]; running?: boolean; stop?: () => void; send?: (t: string) => void };

function setup(over: Partial<SpecialistCardDeps> = {}, seed = "Brief from the task") {
  const id = openSpecialistBreakout({
    breakoutId: "b-1",
    specialistId: "c1",
    specialistName: "Canvas",
    home: "/w/specialists/canvas",
    parentSessionId: "s1",
    parentFile: "/w/proj",
    parentTitle: "Publish Q4 canvas",
    seed,
    startParams: { specialist: "c1:breakout:b-1" },
  });
  const log: unknown[][] = [];
  let props: Record<string, unknown> = {};
  const Spy = (p: Record<string, unknown>) => {
    props = p;
    return null;
  };
  const deps: SpecialistCardDeps = {
    closePrompt: async (s, b) => (log.push(["close-prompt", s, b]), { message: "CLOSE_OUT" }),
    breakoutClosed: async (s, b, sid) => (log.push(["closed", s, b, sid]), { draft: "Outcome: solved", label: "Draft from the Canvas specialist · not sent yet", note_id: "n-1" }),
    discardBreakout: async (s, b) => (log.push(["discard", s, b]), { ok: true as const }),
    deliver: async (d: ComposerDraftDetail) => (log.push(["deliver", d]), true),
    detail: async () => ({ model: "claude-haiku-4-5", face: "#123456" }),
    ...over,
  };
  const card = () => getSpecialistBreakouts().find((c) => c.id === id)!;
  let r!: ReactTestRenderer;
  act(() => {
    r = create(<SpecialistBreakoutCard card={card()} index={0} mount={Spy as never} deps={deps} />, {
      createNodeMock: () => ({ getBoundingClientRect: () => ({ left: 0, top: 0, width: 400, height: 500 }) }),
    });
  });
  mounted.push(r);
  const report = (s: Report) => act(() => (props.onBreakoutState as (s: Report) => void)(s));
  return { r, id, log, props: () => props, report };
}
const flush = () =>
  act(async () => {
    await new Promise((res) => setTimeout(res, 0));
  });
async function ready(over: Partial<SpecialistCardDeps> = {}, seed?: string) {
  const out = setup(over, seed);
  await flush();
  return out;
}

test("the chat runs in the home as a specialist session, sending the brief once", async () => {
  const { props, r } = await ready();
  expect(props()).toMatchObject({
    file: "/w/specialists/canvas",
    extraParams: { specialist: "c1:breakout:b-1" },
    specialist: { id: "c1", kind: "breakout", name: "Canvas", taskTitle: "Publish Q4 canvas" },
    breakoutPrompt: "Brief from the task",
    // The specialist's model is the chat's default; who is being replied to.
    model: "claude-haiku-4-5",
    placeholder: "Reply to the Canvas specialist",
  });
  // Never the research-only posture.
  expect("breakout" in props()).toBe(false);
  act(() => (props().onBreakoutPromptSent as () => void)());
  expect("seed" in getSpecialistBreakouts()[0]!).toBe(false);
  expect(textOf(r.root)).toContain("Breakout from Publish Q4 canvas");
  expect(textOf(r.root)).toContain("Canvas specialist");
});

test("Close: close-prompt into the breakout, then the summary as a labelled draft, then the card goes", async () => {
  const sent: string[] = [];
  const { r, log, report } = await ready();
  report({ sessionId: "bs1", turns: [{ role: "user" }], running: false, send: (t) => sent.push(t) });
  await act(async () => {
    button(r, "Close").props.onClick();
  });
  expect(log[0]).toEqual(["close-prompt", "c1", "b-1"]);
  expect(sent).toEqual(["CLOSE_OUT"]);
  report({ sessionId: "bs1", turns: [], running: true, send: (t) => sent.push(t) });
  await act(async () => {
    report({ sessionId: "bs1", turns: [], running: false, send: (t) => sent.push(t) });
    await new Promise((res) => setTimeout(res, 0));
  });
  expect(log[1]).toEqual(["closed", "c1", "b-1", "bs1"]);
  expect(log[2]).toEqual([
    "deliver",
    {
      sessionId: "s1",
      file: "/w/proj",
      text: "Outcome: solved",
      label: "Draft from the Canvas specialist · not sent yet",
      name: "Canvas",
      breakoutId: "b-1",
    },
  ]);
  expect(getSpecialistBreakouts()).toHaveLength(0);
});

test("Close while it is still replying waits; a failed hand-off keeps the card", async () => {
  const { r, log, report } = await ready({ deliver: async () => false });
  report({ sessionId: "bs1", turns: [{ role: "user" }], running: true, send: () => {} });
  await act(async () => {
    button(r, "Close").props.onClick();
  });
  expect(log).toEqual([]);
  expect(textOf(r.root)).toContain("Wait for the specialist");
  report({ sessionId: "bs1", turns: [{ role: "user" }], running: false, send: () => {} });
  await act(async () => {
    button(r, "Close").props.onClick();
  });
  report({ sessionId: "bs1", turns: [], running: true });
  await act(async () => {
    report({ sessionId: "bs1", turns: [], running: false });
    await new Promise((res) => setTimeout(res, 0));
  });
  expect(getSpecialistBreakouts()).toHaveLength(1);
  expect(textOf(r.root)).toContain("Could not put the summary");
});

test("Discard: the card's own confirm, then recorded, the run stopped, no draft", async () => {
  let stopped = 0;
  const { r, log, report } = await ready();
  report({ sessionId: "bs1", turns: [{ role: "user" }], running: true, stop: () => stopped++ });
  await act(async () => {
    button(r, "Discard").props.onClick();
  });
  // Nothing yet: the card asks first (never the browser's confirm).
  expect(log).toEqual([]);
  expect(r.root.findAll((n) => n.props.role === "alertdialog")).toHaveLength(1);
  await act(async () => {
    button(r, "Discard breakout").props.onClick();
  });
  expect(log).toEqual([["discard", "c1", "b-1"]]);
  expect(stopped).toBe(1);
  expect(getSpecialistBreakouts()).toHaveLength(0);
});

test("Discard declined at the confirm does nothing", async () => {
  const { r, log } = await ready();
  await act(async () => {
    button(r, "Discard").props.onClick();
  });
  await act(async () => {
    button(r, "Keep it").props.onClick();
  });
  expect(log).toEqual([]);
  expect(getSpecialistBreakouts()).toHaveLength(1);
  expect(r.root.findAll((n) => n.props.role === "alertdialog")).toHaveLength(0);
});

test("Close cannot be triggered twice; the head says it is writing the summary", async () => {
  const sent: string[] = [];
  const { r, log, report } = await ready();
  report({ sessionId: "bs1", turns: [{ role: "user" }], running: false, send: (t) => sent.push(t) });
  await act(async () => {
    button(r, "Close").props.onClick();
    button(r, "Close").props.onClick();
  });
  expect(log.filter((x) => x[0] === "close-prompt")).toHaveLength(1);
  expect(sent).toEqual(["CLOSE_OUT"]);
  expect(textOf(r.root)).toContain("Writing the summary…");
  expect(button(r, "Close").props.disabled).toBe(true);
});

test("a deleted specialist: the card says so and can be dismissed (410 reason deleted)", async () => {
  const gone = Object.assign(new Error("This specialist was deleted."), { status: 410, body: { reason: "deleted" } });
  const { r, props } = await ready({ detail: async () => Promise.reject(gone) });
  expect(textOf(r.root)).toContain("This specialist was deleted");
  expect(props()).toEqual({});
  await act(async () => {
    button(r, "Dismiss").props.onClick();
  });
  expect(getSpecialistBreakouts()).toHaveLength(0);
});

test("a specialist deleted while the card is open: the card finds out by itself (seen live)", async () => {
  const gone = Object.assign(new Error("This specialist was deleted."), { status: 410, body: { reason: "deleted" } });
  let alive = true;
  const ticks: (() => void)[] = [];
  const real = globalThis.setInterval;
  globalThis.setInterval = ((fn: () => void, ms: number) => (ms === DELETED_POLL_MS && ticks.push(fn), 1)) as never;
  try {
    const { r } = await ready({ detail: async () => (alive ? { model: "m" } : Promise.reject(gone)) });
    expect(textOf(r.root)).not.toContain("This specialist was deleted");
    alive = false;
    expect(ticks.length).toBeGreaterThan(0);
    await act(async () => {
      ticks[ticks.length - 1]!();
      await new Promise((res) => setTimeout(res, 0));
    });
    expect(textOf(r.root)).toContain("This specialist was deleted");
    expect(button(r, "Dismiss")).toBeTruthy();
  } finally {
    globalThis.setInterval = real;
  }
});

test("Close on a specialist deleted meanwhile turns the card into the deleted state", async () => {
  const gone = Object.assign(new Error("This specialist was deleted."), { status: 410, body: { reason: "deleted" } });
  const { r, report } = await ready({ closePrompt: async () => Promise.reject(gone) });
  report({ sessionId: "bs1", turns: [{ role: "user" }], running: false, send: () => {} });
  await act(async () => {
    button(r, "Close").props.onClick();
  });
  expect(textOf(r.root)).toContain("This specialist was deleted");
  await act(async () => {
    button(r, "Close").props.onClick();
  });
  expect(getSpecialistBreakouts()).toHaveLength(0);
});

describe("where the card opens", () => {
  const view = { w: 1440, h: 900 };
  const chat = { left: 1040, top: 48, right: 1440, bottom: 880 };
  test("beside the task's chat, over the middle pane, not on top of it", () => {
    const b = cardBox(chat, 0, view);
    expect(b.x + b.w).toBeLessThanOrEqual(chat.left);
    expect(b.x).toBeGreaterThanOrEqual(0);
    expect(b.y + b.h).toBeLessThanOrEqual(view.h);
  });
  test("a second card steps down past the first one's head", () => {
    const a = cardBox(chat, 0, view);
    const b = cardBox(chat, 1, view);
    expect(b.y - a.y).toBeGreaterThanOrEqual(48);
  });
  test("always inside the window", () => {
    const small = { w: 800, h: 600 };
    for (let i = 0; i < 6; i++) {
      const b = cardBox({ left: 0, top: 40, right: 800, bottom: 600 }, i, small);
      expect(b.x).toBeGreaterThanOrEqual(0);
      expect(b.y).toBeGreaterThanOrEqual(0);
      expect(b.x + b.w).toBeLessThanOrEqual(small.w);
      expect(b.y + b.h).toBeLessThanOrEqual(small.h);
    }
  });
});

// Seen live: the Discard confirm sat in the flow under the card's chat, which is
// absolutely placed over the whole body, so it could be neither seen nor pressed.
describe("the Discard confirm is above the card's chat", () => {
  test("it is positioned over the body with a z-index", async () => {
    const { readFileSync } = await import("node:fs");
    const css = readFileSync(new URL("./specialist-card.css", import.meta.url), "utf8");
    const rule = /\.spx-card-confirm\s*\{([^}]*)\}/.exec(css)?.[1] ?? "";
    expect(rule).toMatch(/position:\s*absolute/);
    expect(rule).toMatch(/z-index:\s*\d+/);
  });
});

describe("polish: the header's task title", () => {
  test("a long title is cut at a word near 40 characters; a short one is kept", () => {
    const long = 'VB67247: First call mcp__fused_render_specialist_breakout with specialist "Tiles", problem "tile_bounds"';
    expect(clipTitle(long)).toBe("VB67247: First call…");
    expect(clipTitle("Publish the Q4 canvas and check the sync log today")).toBe("Publish the Q4 canvas and check the…");
    expect(clipTitle("Publish Q4 canvas")).toBe("Publish Q4 canvas");
    expect(clipTitle("Publish the Q4 canvas and check the sync log today").length).toBeLessThanOrEqual(41);
  });
});
