// THE CARD WATCH: the reply ended (`done`) while a backgrounded task keeps the
// host alive (`tasks_pending`), and that task raises a permission card later —
// with no poll loop left to see it. The controller keeps asking the agent's
// `cards` action until the host has nothing left running.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, test } from "bun:test";

const { CARD_WATCH_MS, createChatController } = await import("./run-controller");
const { createMemoryParamsStore } = await import("../params/store");

import type { runAgent } from "./agent";
import type { CardsResponse, PermissionRow, PollResponse, Segment, TaskRecord } from "./types";

type Handler = (fields: Record<string, unknown>, call: number) => unknown;

function fakeAgent(handlers: Record<string, Handler>) {
  const calls: { action: string; fields: Record<string, unknown> }[] = [];
  const counts: Record<string, number> = {};
  const run = ((action: string, fields: Record<string, unknown>) => {
    calls.push({ action, fields });
    const n = (counts[action] = (counts[action] || 0) + 1) - 1;
    const h = handlers[action];
    if (!h) throw new Error("fake agent has no handler for " + action);
    return Promise.resolve(h(fields, n));
  }) as unknown as typeof runAgent;
  return { run, calls, of: (a: string) => calls.filter((c) => c.action === a) };
}

function poll(over: Partial<PollResponse> = {}): PollResponse {
  return {
    text: "",
    done: false,
    session_id: "s1",
    error: "",
    tokens: 0,
    phase: "composing",
    message: "",
    permissions: [],
    app_state: [],
    mode: "prompt",
    skills: [],
    retry: null,
    retry_total: 0,
    retry_status: 0,
    cancelled: false,
    tasks_pending: false,
    activity: { tool: null, tools_open: 0, hook: "", tool_input_bytes: 0, tasks: [] },
    segments: [],
    ...over,
  } as PollResponse;
}

const text = (t: string): Segment => ({ kind: "text", id: t, text: t }) as Segment;

const permRow = (over: Partial<PermissionRow> = {}): PermissionRow => ({
  id: "p1",
  tool: "Bash",
  input: { command: "rm -rf build" },
  created_at: 1,
  decision: "",
  scope: "",
  mode: "",
  answers: {},
  ...over,
});

const task = (id: string): TaskRecord =>
  ({
    id,
    description: id,
    kind: "agent",
    type: "local_agent",
    subagent_type: "general-purpose",
    isolation: "",
    tool_use_id: "",
    status: "running",
    started_at: 1,
    ended_at: null,
    backgrounded: true,
    usage: null,
    error: "",
    summary: "",
    events: 0,
  }) as TaskRecord;

const cards = (over: Partial<CardsResponse> = {}): CardsResponse => ({
  alive: true,
  tasks_pending: true,
  permissions: [],
  mode: "prompt",
  ...over,
});

/** Settle every queued microtask (the fake agent answers on the microtask
 *  queue, so a handful of macrotask turns is plenty). */
const flush = async () => {
  for (let i = 0; i < 5; i++) await new Promise((r) => setTimeout(r, 0));
};

/**
 * A controller whose card-watch sleeps are GATED: the watch parks on its
 * `CARD_WATCH_MS` sleep until the test calls `tick()`; every other sleep (the
 * poll loop's) resolves at once.
 */
function makeController(handlers: Record<string, Handler>) {
  const agent = fakeAgent(handlers);
  const gates: (() => void)[] = [];
  const controller = createChatController({
    file: "/proj/app.py",
    params: createMemoryParamsStore(),
    run: agent.run,
    sleep: (ms: number) =>
      ms === CARD_WATCH_MS ? new Promise<void>((r) => gates.push(r)) : Promise.resolve(),
    now: () => 1_000,
    hasPane: () => true,
  });
  const tick = async () => {
    await flush();
    const open = gates.splice(0);
    for (const g of open) g();
    await flush();
  };
  return { controller, agent, tick, gates };
}

const doneWithTasks = () =>
  poll({ done: true, text: "ok", segments: [text("ok")], tasks_pending: true, tasks: [task("t1")] });

describe("the card watch after a reply ends with a background task running", () => {
  test("an open card the task raises lands open in the tail pin, line says awaiting", async () => {
    const { controller, agent, tick } = makeController({
      poll: (_f, n) => (n === 0 ? poll() : doneWithTasks()),
      cards: (_f, n) => (n === 0 ? cards() : cards({ permissions: [permRow()] })),
    });
    await controller.resumeRun("r1");
    expect(controller.getState().working).toBeNull();
    // Still running: the done branch kept the run's task records.
    expect(controller.getState().tasks[0]?.status).toBe("running");

    await tick();
    expect(agent.of("cards")).toHaveLength(1);
    expect(agent.of("cards")[0].fields).toEqual({ run_id: "r1" });
    expect(controller.getState().permissions).toHaveLength(0);

    await tick();
    const rows = controller.getState().permissions;
    expect(rows).toHaveLength(1);
    expect(rows[0].placement).toBe("open");
    expect(rows[0].runId).toBe("r1");
    const w = controller.getState().working;
    expect(w?.phase).toBe("awaiting");
    // No turn to stop: the line is external (no Stop button).
    expect(w?.external).toBe(true);
  });

  test("a decision parks the card and clears the line; the watch keeps going", async () => {
    const { controller, agent, tick } = makeController({
      poll: (_f, n) => (n === 0 ? poll() : doneWithTasks()),
      cards: (_f, n) =>
        n === 0 ? cards({ permissions: [permRow()] }) : cards({ permissions: [permRow({ decision: "allow" })] }),
      decide: () => ({ decided: "p1", decision: "allow", scope: "once", mode: "", answers: {} }),
    });
    await controller.resumeRun("r1");
    await tick();
    expect(controller.getState().working?.phase).toBe("awaiting");

    await controller.decidePermission("p1", "allow", "once");
    expect(agent.of("decide")[0].fields.run_id).toBe("r1");
    expect(controller.getState().permissions[0].placement).toBe("parked");
    expect(controller.getState().working).toBeNull();

    // The replayed (now decided) row keeps it parked and the line down.
    await tick();
    expect(agent.of("cards")).toHaveLength(2);
    expect(controller.getState().permissions[0].placement).toBe("parked");
    expect(controller.getState().working).toBeNull();
  });

  test("`tasks_pending: false` ends the watch and retires the run's tasks", async () => {
    const { controller, agent, tick } = makeController({
      poll: (_f, n) => (n === 0 ? poll() : doneWithTasks()),
      cards: (_f, n) => (n === 0 ? cards() : cards({ tasks_pending: false })),
    });
    await controller.resumeRun("r1");
    await tick();
    expect(controller.getState().tasks[0]?.status).toBe("running");
    await tick();
    expect(controller.getState().tasks[0]?.status).toBe("unknown");
    await tick();
    await tick();
    expect(agent.of("cards")).toHaveLength(2);
  });

  test("a new poll loop for the run (the task woke the model) stops the watch", async () => {
    let release!: () => void;
    const woke = new Promise<void>((r) => (release = r));
    const { controller, agent, tick } = makeController({
      poll: async (_f, n) => {
        // resumeRun's probe, then the loop's done; the second resumeRun's probe.
        if (n === 0 || n === 2) return poll();
        if (n === 1) return doneWithTasks();
        // The woken turn: held open until the test lets it end.
        await woke;
        return poll({ done: true, text: "woke", segments: [text("woke")] });
      },
      cards: () => cards({ permissions: [permRow()] }),
    });
    await controller.resumeRun("r1");
    await tick();
    expect(agent.of("cards")).toHaveLength(1);

    const resumed = controller.resumeRun("r1");
    await flush();
    expect(controller.hasActiveRun()).toBe(true);
    await tick(); // the watch wakes, sees the live loop, leaves without asking
    expect(agent.of("cards")).toHaveLength(1);
    release();
    await resumed;
    await tick();
    await tick();
    expect(agent.of("cards")).toHaveLength(1);
  });

  test("a second done for the same run while watching starts no second watch", async () => {
    const { controller, agent, tick, gates } = makeController({
      poll: (_f, n) => (n === 0 ? poll() : doneWithTasks()),
      cards: () => cards(),
    });
    await controller.resumeRun("r1");
    await flush();
    expect(gates).toHaveLength(1);
    await controller.resumeRun("r1");
    await flush();
    // The first watch saw the loop come and go — still exactly one parked sleep.
    expect(gates).toHaveLength(1);
    await tick();
    expect(agent.of("cards")).toHaveLength(1);
  });

  test("a reload onto a reply that already ended (the probe road) watches too", async () => {
    const { controller, agent, tick } = makeController({
      poll: () => doneWithTasks(),
      cards: () => cards({ permissions: [permRow()] }),
    });
    await controller.resumeRun("r1");
    await tick();
    expect(agent.of("cards")).toHaveLength(1);
    expect(controller.getState().permissions[0]?.placement).toBe("open");
    expect(controller.getState().working?.phase).toBe("awaiting");
  });

  test("a history restore onto a live host with work running watches too", async () => {
    const { controller, agent, tick } = makeController({
      history: () => ({
        turns: [],
        transcript: null,
        live_run: "",
        tasks_run: "r1",
        tasks: [task("t1")],
      }),
      live_run: () => ({ run_id: "" }),
      live_host: () => ({ run_id: "" }),
      tasks: () => ({ tasks: [task("t1")], host_alive: false, tasks_pending: false }),
      cards: () => cards({ permissions: [permRow()] }),
    });
    await controller.openSession("s9");
    await tick();
    expect(agent.of("cards")).toHaveLength(1);
    expect(controller.getState().permissions[0]?.placement).toBe("open");
  });

  test("done without tasks_pending starts no watch", async () => {
    const { controller, agent, tick, gates } = makeController({
      poll: (_f, n) => (n === 0 ? poll() : poll({ done: true, text: "ok", segments: [text("ok")] })),
      cards: () => cards(),
    });
    await controller.resumeRun("r1");
    await tick();
    await tick();
    expect(gates).toHaveLength(0);
    expect(agent.of("cards")).toHaveLength(0);
  });
});
