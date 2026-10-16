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

function make(
  handlers: Record<string, Handler>,
  schedule?: (body: unknown) => Promise<unknown>,
  cancelSchedule?: (id: string) => Promise<unknown>,
) {
  const counts: Record<string, number> = {};
  const run = ((action: string, fields: Record<string, unknown>) => {
    const n = (counts[action] = (counts[action] || 0) + 1) - 1;
    const h = handlers[action];
    if (!h) throw new Error("no handler for " + action);
    return Promise.resolve(h(fields, n));
  }) as unknown as typeof runAgent;
  const controller = createChatController({
    file: "/proj/app.py",
    params: createMemoryParamsStore(),
    run,
    sleep: () => Promise.resolve(),
    now: () => 1_000,
    hasPane: () => true,
    ...(schedule ? { schedule: schedule as never } : {}),
    ...(cancelSchedule ? { cancelSchedule } : {}),
  });
  return controller;
}

const flush = () => new Promise<void>((r) => setTimeout(r, 0));

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

  test("a refused POST keeps the limit card (with its reset) and notes the refusal", async () => {
    const c = make(
      {
        start: () => ({ run_id: "r1" }),
        poll: () => poll({ done: true, error: LIMIT_TEXT, quota: rejected() }),
      },
      () => Promise.reject(new Error("target: refused")),
    );
    await c.sendMessage("go");
    await flush();
    const state = c.getState();
    // ONE error row and the card still the limit's: the refusal must not
    // replace the card that carries the reset time (Bugbot #1107).
    expect(state.turns.filter((t) => t.role === "error").length).toBe(1);
    expect(state.trouble?.kind).toBe("limit");
    expect(state.trouble?.quota?.resets_at).toBe(RESET);
    expect(state.trouble?.scheduled).toBeUndefined();
    const notes = state.turns.filter((t): t is NoteTurn => t.role === "note");
    expect(notes.length).toBe(1);
    expect(notes[0]!.text).toContain("Could not schedule the follow-up");
  });

  test("a second end on the same run prints nothing and posts nothing", async () => {
    const posted: unknown[] = [];
    const c = make(
      {
        start: () => ({ run_id: "r1" }),
        poll: () => poll({ done: true, error: LIMIT_TEXT, quota: rejected() }),
      },
      (body) => {
        posted.push(body);
        return Promise.resolve({});
      },
    );
    await c.sendMessage("go");
    await flush();
    // Re-attach to the SAME run id: agent.py answers with the same ended poll.
    await c.resumeRun("r1");
    await flush();
    expect(posted.length).toBe(1);
    expect(c.getState().turns.filter((t) => t.role === "error").length).toBe(1);
  });

  test("a limit hit repaired off-frame gets the card and the comeback too", async () => {
    const posted: unknown[] = [];
    const c = make(
      {
        poll: () => poll({ done: true, message: "do it", error: LIMIT_TEXT, quota: rejected() }),
      },
      (body) => {
        posted.push(body);
        return Promise.resolve({});
      },
    );
    await c.resumeRun("r9");
    await flush();
    expect(posted.length).toBe(1);
    expect((posted[0] as { session_id: string }).session_id).toBe("s1");
    const state = c.getState();
    expect(state.trouble?.kind).toBe("limit");
    expect(state.trouble?.quota?.resets_at).toBe(RESET);
    // Empty transcript → the repair's own rule adds no user line (same as
    // `addError` did); the card row and the comeback note are what matter.
    expect(state.turns.map((t) => t.role)).toEqual(["error", "note"]);
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

describe("after the limit", () => {
  test("a reply that lands retires the limit row, its card, and the booked comeback", async () => {
    // Turn 1 dies on the limit; turn 2 (the reader signed into another account
    // and retried) completes. The red row above a reply that plainly arrived
    // is a scar, not a report — it goes, the card goes, and the scheduled
    // "Your usage limit has reset…" is cancelled rather than fired into a chat
    // that has already moved on.
    let turn = 0;
    const cancelled: string[] = [];
    const c = make(
      {
        start: () => ({ run_id: "r" + ++turn }),
        poll: () =>
          turn === 1
            ? poll({ done: true, error: LIMIT_TEXT, quota: rejected() })
            : poll({ done: true, text: "None found." }),
      },
      () => Promise.resolve({ entry: { id: "e1" } }),
      (id) => {
        cancelled.push(id);
        return Promise.resolve({});
      },
    );
    await c.sendMessage("any regressions?");
    await flush();
    expect(c.getState().turns.map((t) => t.role)).toEqual(["user", "error", "note"]);
    await c.sendMessage("any regressions?");
    await flush();
    const state = c.getState();
    expect(state.trouble).toBeNull();
    expect(state.turns.map((t) => t.role)).toEqual(["user", "note", "user", "assistant"]);
    expect(cancelled).toEqual(["e1"]);
  });

  test("an empty end after the limit retires nothing", async () => {
    // No text, no error: nothing proves the limit lifted, and the server would
    // keep the row too (no reply after it) — live and reload must agree.
    let turn = 0;
    const cancelled: string[] = [];
    const c = make(
      {
        start: () => ({ run_id: "r" + ++turn }),
        poll: () =>
          turn === 1 ? poll({ done: true, error: LIMIT_TEXT, quota: rejected() }) : poll({ done: true }),
      },
      () => Promise.resolve({ entry: { id: "e1" } }),
      (id) => {
        cancelled.push(id);
        return Promise.resolve({});
      },
    );
    await c.sendMessage("go");
    await flush();
    await c.sendMessage("go");
    await flush();
    expect(c.getState().turns.map((t) => t.role)).toEqual(["user", "error", "note", "user"]);
    expect(cancelled).toEqual([]);
  });

  test("a Claude rate-limit break (not the plan window) is kept after a reply", async () => {
    // The trouble classifier files any Claude "rate limit" sentence as `limit`
    // too; only the PLAN's window is a clock. A 429 is a break and stays, as
    // agent.py `_drop_stale_limit_errors` keeps it on reload.
    let turn = 0;
    const c = make({
      start: () => ({ run_id: "r" + ++turn }),
      poll: () =>
        turn === 1
          ? poll({ done: true, error: "Claude API rate limit exceeded (429)" })
          : poll({ done: true, text: "Second attempt worked." }),
    });
    await c.sendMessage("go");
    await flush();
    expect((c.getState().turns[1] as { kind?: string }).kind).toBe("limit");
    await c.sendMessage("go");
    await flush();
    expect(c.getState().turns.map((t) => t.role)).toEqual(["user", "error", "user", "assistant"]);
  });

  test("a reply after an ordinary failure keeps the failure's row", async () => {
    let turn = 0;
    const c = make({
      start: () => ({ run_id: "r" + ++turn }),
      poll: () =>
        turn === 1
          ? poll({ done: true, error: "API Error: overloaded" })
          : poll({ done: true, text: "Second attempt worked." }),
    });
    await c.sendMessage("go");
    await flush();
    await c.sendMessage("go");
    await flush();
    expect(c.getState().turns.map((t) => t.role)).toEqual(["user", "error", "user", "assistant"]);
  });
});
