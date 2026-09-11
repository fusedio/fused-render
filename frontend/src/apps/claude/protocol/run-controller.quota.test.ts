// THE COMEBACK AFTER A USAGE LIMIT (run-controller `scheduleComeback`).
//
// A headless `claude -p` run that hits the plan limit ends its turn with the
// CLI's own sentence as `error` and `quota.status === "rejected"` beside it —
// the CLI's TUI would now wait and continue on its own; `-p` does not. So the
// controller schedules ONE message on the same session at the reset the CLI
// reported, carrying the fixed continuation prompt, and says so in the log.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, test } from "bun:test";

const { createChatController } = await import("./run-controller");
const { createMemoryParamsStore } = await import("../params/store");
const { CONTINUE_GRACE_S, CONTINUE_PROMPT, CONTINUE_TITLE } = await import("./quota");

import type { runAgent } from "./agent";
import type { NoteTurn } from "./controller-api";
import type { PollResponse, Quota } from "./types";

const RESET = 1789066800;
const LIMIT_TEXT = "You've hit your session limit · resets 12:30am (Asia/Calcutta)";

const rejected = (): Quota => ({
  status: "rejected",
  type: "five_hour",
  resets_at: RESET,
  utilization: null,
  windows: { five_hour: { utilization: 1, resets_at: RESET } },
});

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
    activity: {
      tool: null,
      tools_open: 0,
      tool_input_bytes: 0,
      thinking_tokens: 0,
      hook: "",
      tasks: [],
      agent_rows: 0,
    },
    segments: [],
    ...over,
  };
}

type Handler = (fields: Record<string, unknown>, call: number) => unknown;

function make(handlers: Record<string, Handler>, schedule?: (body: unknown) => Promise<unknown>) {
  const counts: Record<string, number> = {};
  const run = ((_dir: string, action: string, fields: Record<string, unknown>) => {
    const n = (counts[action] = (counts[action] || 0) + 1) - 1;
    const h = handlers[action];
    if (!h) throw new Error("no handler for " + action);
    return Promise.resolve(h(fields, n));
  }) as unknown as typeof runAgent;
  const controller = createChatController({
    file: "/proj/app.py",
    agentDir: "/tpl/claude",
    params: createMemoryParamsStore(),
    run,
    sleep: () => Promise.resolve(),
    now: () => 1_000,
    hasPane: () => true,
    ...(schedule ? { schedule: schedule as never } : {}),
  });
  return controller;
}

const flush = () => new Promise<void>((r) => setTimeout(r, 0));

describe("the plan window on the poll", () => {
  test("rides into state and survives a poll without one", async () => {
    const warn: Quota = { ...rejected(), status: "allowed_warning", utilization: 0.93 };
    const c = make({
      start: () => ({ run_id: "r1" }),
      poll: (_f, n) => (n === 0 ? poll({ quota: warn }) : poll({ done: true, text: "ok" })),
    });
    await c.sendMessage("hi");
    expect(c.getState().quota).toEqual(warn);
  });
});

describe("a limit hit", () => {
  test("schedules the comeback on the session at the reset, and notes it", async () => {
    const posted: unknown[] = [];
    const c = make(
      {
        start: () => ({ run_id: "r1" }),
        poll: () => poll({ done: true, error: LIMIT_TEXT, quota: rejected() }),
      },
      (body) => {
        posted.push(body);
        return Promise.resolve({ entry: { id: "e1" } });
      },
    );
    await c.sendMessage("do the thing");
    await flush();
    expect(posted).toEqual([
      {
        target: "/proj/app.py",
        message: CONTINUE_PROMPT,
        due: new Date((RESET + CONTINUE_GRACE_S) * 1000).toISOString(),
        session_id: "s1",
        title: CONTINUE_TITLE,
      },
    ]);
    const state = c.getState();
    expect(state.trouble?.kind).toBe("limit");
    expect(state.trouble?.quota?.resets_at).toBe(RESET);
    expect(state.trouble?.scheduled).toBe(true);
    const roles = state.turns.map((t) => t.role);
    expect(roles).toEqual(["user", "error", "note"]);
    const note = state.turns[2] as NoteTurn;
    expect(note.glyph).toBe("◷");
    expect(note.text).toStartWith("Usage limit reached · continuing automatically at ");
    // The error row carries the window too, for a renderer that wants the time.
    expect((state.turns[1] as { quota?: Quota }).quota?.status).toBe("rejected");
  });

  test("a refused POST leaves the limit card up and says the follow-up did not schedule", async () => {
    const c = make(
      {
        start: () => ({ run_id: "r1" }),
        poll: () => poll({ done: true, error: LIMIT_TEXT, quota: rejected() }),
      },
      () => Promise.reject(new Error("target: refused")),
    );
    await c.sendMessage("go");
    await flush();
    const errs = c.getState().turns.filter((t) => t.role === "error");
    expect(errs.length).toBe(2);
    expect((errs[1] as { text: string }).text).toContain("Could not schedule the follow-up");
    expect(c.getState().turns.some((t) => t.role === "note")).toBe(false);
  });

  test("an error without a rejected window is an ordinary failure — nothing scheduled", async () => {
    const posted: unknown[] = [];
    const c = make(
      {
        start: () => ({ run_id: "r1" }),
        poll: () =>
          poll({ done: true, error: "API Error: 529 Overloaded", quota: { ...rejected(), status: "allowed" } }),
      },
      (body) => {
        posted.push(body);
        return Promise.resolve({});
      },
    );
    await c.sendMessage("go");
    await flush();
    expect(posted).toEqual([]);
    expect(c.getState().trouble?.quota).toBeUndefined();
  });
});

describe("a run repaired after the fact", () => {
  test("still hands its plan window to state — the pill for a comeback that finished off-frame", async () => {
    const warn: Quota = { ...rejected(), status: "allowed_warning", utilization: 0.93 };
    const c = make({
      poll: () => poll({ done: true, message: "continue", text: "back at it", quota: warn }),
    });
    await c.resumeRun("r1");
    expect(c.getState().quota).toEqual(warn);
  });
});
