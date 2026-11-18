// The specialist breakout card (docs/specialists PLAN C.4): the chat it mounts,
// the close flow that drafts the summary into the task's composer, and Discard.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";

import {
  getSpecialistBreakouts,
  openSpecialistBreakout,
  resetSpecialistBreakouts,
} from "@platform/lib/specialist-breakouts";
import type { ComposerDraftDetail } from "@platform/lib/breakouts";
import { SpecialistBreakoutCard, type SpecialistCardDeps } from "./SpecialistBreakoutCard";

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
    confirm: () => true,
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

test("the chat runs in the home as a specialist session, sending the brief once", () => {
  const { props, r } = setup();
  expect(props()).toMatchObject({
    file: "/w/specialists/canvas",
    extraParams: { specialist: "c1:breakout:b-1" },
    specialist: { id: "c1", kind: "breakout" },
    breakoutPrompt: "Brief from the task",
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
  const { r, log, report } = setup();
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
    { sessionId: "s1", file: "/w/proj", text: "Outcome: solved", label: "Draft from the Canvas specialist · not sent yet" },
  ]);
  expect(getSpecialistBreakouts()).toHaveLength(0);
});

test("Close while it is still replying waits; a failed hand-off keeps the card", async () => {
  const { r, log, report } = setup({ deliver: async () => false });
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

test("Discard: confirmed, recorded, the run stopped, no draft", async () => {
  let stopped = 0;
  const { r, log, report } = setup();
  report({ sessionId: "bs1", turns: [{ role: "user" }], running: true, stop: () => stopped++ });
  await act(async () => {
    button(r, "Discard").props.onClick();
  });
  expect(log).toEqual([["discard", "c1", "b-1"]]);
  expect(stopped).toBe(1);
  expect(getSpecialistBreakouts()).toHaveLength(0);
});

test("Discard declined at the confirm does nothing", async () => {
  const { r, log } = setup({ confirm: () => false });
  await act(async () => {
    button(r, "Discard").props.onClick();
  });
  expect(log).toEqual([]);
  expect(getSpecialistBreakouts()).toHaveLength(1);
});
