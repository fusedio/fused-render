// The heads-up poller's contract: when it asks, when it stops, and what the
// card's buttons do. fetch is replaced LOCALLY per test (mock.module is
// process-wide in bun and would break unrelated suites).
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

const { useHeadsUp, nextPollDelay, RUNNING_MS, CHECKING_MS, SETTLE_MS } = await import("./useHeadsUp");
import type { HeadsUpApi } from "./useHeadsUp";

const realFetch = globalThis.fetch;
let polls: string[] = [];
let posts: unknown[] = [];
let answers: unknown[] = [];

beforeEach(() => {
  polls = [];
  posts = [];
  answers = [];
  (globalThis as { fetch: unknown }).fetch = (url: string, init?: { method?: string; body?: string }) => {
    if (init?.method === "POST") {
      posts.push(JSON.parse(init.body ?? "{}"));
      return Promise.resolve(new Response("{}", { status: 200 }));
    }
    polls.push(String(url));
    const next = answers.shift() ?? { state: "idle" };
    return Promise.resolve(new Response(JSON.stringify(next), { status: 200 }));
  };
});

const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  (globalThis as { fetch: unknown }).fetch = realFetch;
});

const NOTE = { learn: "x", tag: "Heads up", title: "T", bullets: ["one", "two"] };

test("nextPollDelay: 10 s while running, 5 s while checking, one settle poll, then stop", () => {
  expect(nextPollDelay({ running: true, state: "idle", ended: false })).toBe(RUNNING_MS);
  expect(nextPollDelay({ running: true, state: "checking", ended: false })).toBe(CHECKING_MS);
  expect(nextPollDelay({ running: false, state: "checking", ended: false })).toBe(CHECKING_MS);
  expect(nextPollDelay({ running: false, state: "idle", ended: false })).toBeNull();
  expect(nextPollDelay({ running: false, state: "note", ended: false })).toBeNull();
  expect(nextPollDelay({ running: false, state: "idle", ended: true })).toBe(SETTLE_MS);
  expect(nextPollDelay({ running: true, state: "disabled", ended: false })).toBeNull();
  expect(nextPollDelay({ running: false, state: "disabled", ended: true })).toBeNull();
  expect(RUNNING_MS).toBe(10_000);
  expect(CHECKING_MS).toBe(5_000);
  expect(SETTLE_MS).toBe(1_500);
});

interface Props {
  runId: string;
  running: boolean;
  enabled: boolean;
  userTurns: number;
  controller?: unknown;
}
const fast = { running: 15, checking: 10, settle: 5 };
let api: HeadsUpApi;
function Probe(p: Props) {
  api = useHeadsUp({ ...p, controller: (p.controller ?? {}) as never, timing: fast });
  return null;
}
function mount(p: Props) {
  let r!: ReactTestRenderer;
  act(() => {
    r = create(<Probe {...p} />);
  });
  mounted.push(r);
  return r;
}
const wait = (ms: number) => act(() => new Promise<void>((res) => setTimeout(res, ms)));

test("disabled: never asks", async () => {
  mount({ runId: "r1", running: true, enabled: false, userTurns: 0 });
  await wait(60);
  expect(polls).toEqual([]);
});

test("running: polls repeatedly and surfaces a note; stopped and idle: no more polling", async () => {
  answers = [{ state: "idle" }, { state: "note", id: "n1", note: NOTE }];
  const r = mount({ runId: "r1", running: true, enabled: true, userTurns: 0 });
  await wait(80);
  expect(polls.length).toBeGreaterThanOrEqual(2);
  expect(polls[0]).toContain("/api/claude-sessions/headsup?run_id=r1");
  expect(api.note?.title).toBe("T");
  expect(api.id).toBe("n1");
  act(() => r.update(<Probe runId="r1" running={false} enabled userTurns={0} />));
  await wait(40);
  const after = polls.length;
  await wait(60);
  expect(polls.length).toBe(after);
});

test("stopped but still checking keeps polling until it settles", async () => {
  answers = [{ state: "idle" }];
  const r = mount({ runId: "r1", running: true, enabled: true, userTurns: 0 });
  await wait(30);
  answers = [{ state: "checking" }, { state: "checking" }, { state: "idle" }];
  act(() => r.update(<Probe runId="r1" running={false} enabled userTurns={0} />));
  await wait(120);
  const n = polls.length;
  expect(n).toBeGreaterThanOrEqual(4);
  await wait(60);
  expect(polls.length).toBe(n);
});

test("dismiss and gotIt post to the dismiss endpoint and hide the card", async () => {
  for (const name of ["dismiss", "gotIt"] as const) {
    posts = [];
    answers = [{ state: "note", id: "n1", note: NOTE }];
    const r = mount({ runId: "r1", running: true, enabled: true, userTurns: 0 });
    await wait(40);
    expect(api.note).not.toBeNull();
    await act(async () => {
      await api[name]();
    });
    expect(posts).toEqual([{ run_id: "r1", id: "n1" }]);
    expect(api.note).toBeNull();
    act(() => r.unmount());
    mounted.pop();
  }
});

test("askInChat follows up while running, sends otherwise, then dismisses", async () => {
  const sent: string[] = [];
  const controller = {
    sendFollowUp: async (t: string) => void sent.push("follow:" + t),
    sendMessage: async (t: string) => void sent.push("send:" + t),
  };
  answers = [{ state: "note", id: "n1", note: NOTE }];
  const r = mount({ runId: "r1", running: true, enabled: true, userTurns: 0, controller });
  await wait(40);
  await act(async () => {
    await api.askInChat();
  });
  expect(sent).toEqual(["follow:Tell me more about this: T\n\none\ntwo"]);
  expect(posts.length).toBe(1);
  expect(api.note).toBeNull();

  sent.length = 0;
  answers = [{ state: "note", id: "n2", note: NOTE }];
  act(() => r.update(<Probe runId="r2" running enabled userTurns={0} controller={controller} />));
  await wait(40);
  act(() => r.update(<Probe runId="r2" running={false} enabled userTurns={0} controller={controller} />));
  await act(async () => {
    await api.askInChat();
  });
  expect(sent).toEqual(["send:Tell me more about this: T\n\none\ntwo"]);
});

test("the card clears itself after the user sends two more messages", async () => {
  answers = [{ state: "note", id: "n1", note: NOTE }];
  const r = mount({ runId: "r1", running: true, enabled: true, userTurns: 3 });
  await wait(40);
  expect(api.note).not.toBeNull();
  act(() => r.update(<Probe runId="r1" running enabled userTurns={4} />));
  expect(api.note).not.toBeNull();
  act(() => r.update(<Probe runId="r1" running enabled userTurns={5} />));
  expect(api.note).toBeNull();
});

test("a disabled answer from the server stops polling that run", async () => {
  answers = [{ state: "disabled" }];
  mount({ runId: "r1", running: true, enabled: true, userTurns: 0 });
  await wait(100);
  expect(polls.length).toBe(1);
});

test("a note never carries into another conversation", async () => {
  answers = [{ state: "note", id: "n1", note: NOTE }];
  const r = mount({ runId: "r1", running: true, enabled: true, userTurns: 0 });
  await wait(40);
  expect(api.note).not.toBeNull();
  answers = [{ state: "idle" }];
  act(() => r.update(<Probe runId="r2" running enabled userTurns={0} />));
  expect(api.note).toBeNull();
  await wait(40);
  expect(api.note).toBeNull();
});

test("askInChat keeps the card and does not dismiss when the send fails", async () => {
  const controller = {
    sendFollowUp: async () => {
      throw new Error("offline");
    },
    sendMessage: async () => {
      throw new Error("offline");
    },
  };
  answers = [{ state: "note", id: "n1", note: NOTE }];
  mount({ runId: "r1", running: true, enabled: true, userTurns: 0, controller });
  await wait(40);
  posts = [];
  await act(async () => {
    await api.askInChat(); // must not reject
  });
  expect(api.note).not.toBeNull();
  expect(posts).toEqual([]);
});

test("askInChat on success hides the card and posts the dismiss after the send", async () => {
  const order: string[] = [];
  const controller = {
    sendFollowUp: async () => void order.push("send"),
    sendMessage: async () => void order.push("send"),
  };
  answers = [{ state: "note", id: "n1", note: NOTE }];
  mount({ runId: "r1", running: true, enabled: true, userTurns: 0, controller });
  await wait(40);
  posts = [];
  await act(async () => {
    await api.askInChat();
  });
  expect(order).toEqual(["send"]);
  expect(posts).toEqual([{ run_id: "r1", id: "n1" }]);
  expect(api.note).toBeNull();
});
