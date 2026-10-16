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

type Entry = { id: string; session_id: string; message: string; state: string; created?: string };
function make(
  handlers: Record<string, Handler>,
  schedule?: (body: unknown) => Promise<unknown>,
  cancelSchedule?: (id: string) => Promise<unknown>,
  listSchedule?: () => Promise<{ entries: Entry[] }>,
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
    listSchedule: listSchedule ?? (() => Promise.resolve({ entries: [] })),
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
  test("a reply that lands retires the limit row, its card, and every booked comeback", async () => {
    // Turn 1 dies on the limit; turn 2 (the reader signed into another account
    // and retried) completes. The red row above a reply that plainly arrived
    // is a scar, not a report — it goes, the card goes, and every pending
    // "Your usage limit has reset…" the SERVER holds for this session is
    // cancelled — including one booked by an earlier frame (a reload) — rather
    // than fired into a chat that has already moved on. Other sessions' rows,
    // other messages and rows already sent are left alone.
    let turn = 0;
    const cancelled: string[] = [];
    const entries: Entry[] = [
      { id: "old", session_id: "s1", message: CONTINUE_PROMPT, state: "pending" },
      { id: "e1", session_id: "s1", message: CONTINUE_PROMPT, state: "pending" },
      { id: "sent", session_id: "s1", message: CONTINUE_PROMPT, state: "sent" },
      { id: "other", session_id: "s2", message: CONTINUE_PROMPT, state: "pending" },
      { id: "mine", session_id: "s1", message: "ship it at 5", state: "pending" },
      // Booked AFTER this retire (a retry that hit the limit again while the
      // list was in flight): for a chat that is stuck again, so it stays.
      {
        id: "newer",
        session_id: "s1",
        message: CONTINUE_PROMPT,
        state: "pending",
        created: new Date(Date.now() + 60_000).toISOString(),
      },
      { id: "older", session_id: "s1", message: CONTINUE_PROMPT, state: "pending", created: new Date(0).toISOString() },
    ];
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
      () => Promise.resolve({ entries }),
    );
    await c.sendMessage("any regressions?");
    await flush();
    expect(c.getState().turns.map((t) => t.role)).toEqual(["user", "error", "note"]);
    await c.sendMessage("any regressions?");
    await flush();
    const state = c.getState();
    expect(state.trouble).toBeNull();
    expect(state.turns.map((t) => t.role)).toEqual(["user", "note", "user", "assistant"]);
    expect(cancelled.sort()).toEqual(["e1", "old", "older"]);
  });

  test("a comeback whose POST settles after the reply is cancelled on arrival", async () => {
    // The reader retried before the schedule POST came back: the row it then
    // makes is stale the moment it exists. No note, no "scheduled" card.
    let turn = 0;
    const cancelled: string[] = [];
    let resolvePost: (v: unknown) => void = () => {};
    const c = make(
      {
        start: () => ({ run_id: "r" + ++turn }),
        poll: () =>
          turn === 1
            ? poll({ done: true, error: LIMIT_TEXT, quota: rejected() })
            : poll({ done: true, text: "None found." }),
      },
      () => new Promise((r) => (resolvePost = r)),
      (id) => {
        cancelled.push(id);
        return Promise.resolve({});
      },
      () => Promise.resolve({ entries: [] }),
    );
    await c.sendMessage("go");
    await flush();
    await c.sendMessage("go");
    await flush();
    resolvePost({ entry: { id: "late" } });
    await flush();
    expect(cancelled).toEqual(["late"]);
    expect(c.getState().turns.map((t) => t.role)).toEqual(["user", "user", "assistant"]);
    expect(c.getState().trouble).toBeNull();
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
      () => Promise.resolve({ entries: [{ id: "e1", session_id: "s1", message: CONTINUE_PROMPT, state: "pending" }] }),
    );
    await c.sendMessage("go");
    await flush();
    await c.sendMessage("go");
    await flush();
    expect(c.getState().turns.map((t) => t.role)).toEqual(["user", "error", "note", "user"]);
    expect(cancelled).toEqual([]);
  });

  test("a Claude rate-limit break (not the plan window) is retired without touching the schedule", async () => {
    // The trouble classifier files any Claude "rate limit" sentence as `limit`
    // too; only the PLAN's window books a comeback, so only the plan's window
    // has one to cancel — a 429 row goes like any resolved failure, and the
    // schedule is never fetched for it.
    let turn = 0;
    let listed = 0;
    const c = make(
      {
        start: () => ({ run_id: "r" + ++turn }),
        poll: () =>
          turn === 1
            ? poll({ done: true, error: "Claude API rate limit exceeded (429)" })
            : poll({ done: true, text: "Second attempt worked." }),
      },
      undefined,
      undefined,
      () => {
        listed++;
        return Promise.resolve({ entries: [] });
      },
    );
    await c.sendMessage("go");
    await flush();
    expect((c.getState().turns[1] as { kind?: string }).kind).toBe("limit");
    await c.sendMessage("go");
    await flush();
    expect(c.getState().turns.map((t) => t.role)).toEqual(["user", "user", "assistant"]);
    expect(listed).toBe(0);
  });

  test("a reply after an ordinary failure retires the failure's row and card too", async () => {
    // "The same for other error cards": the overload is resolved by the reply
    // that followed it, and a red row above that reply would read as still wrong.
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
    expect(c.getState().trouble?.kind).toBeDefined();
    await c.sendMessage("go");
    await flush();
    expect(c.getState().turns.map((t) => t.role)).toEqual(["user", "user", "assistant"]);
    expect(c.getState().trouble).toBeNull();
  });

  test("a failure followed only by another failure keeps both", async () => {
    let turn = 0;
    const c = make({
      start: () => ({ run_id: "r" + ++turn }),
      poll: () =>
        poll({ done: true, error: turn === 1 ? "API Error: overloaded" : "API Error: Can't reach the API server" }),
    });
    await c.sendMessage("go");
    await flush();
    await c.sendMessage("go");
    await flush();
    expect(c.getState().turns.map((t) => t.role)).toEqual(["user", "error", "user", "error"]);
  });
});

describe("a tool-only reply", () => {
  test("retires nothing — it answered nothing", async () => {
    let turn = 0;
    let listed = 0;
    const c = make(
      {
        start: () => ({ run_id: "r" + ++turn }),
        poll: () =>
          turn === 1
            ? poll({ done: true, error: LIMIT_TEXT, quota: rejected() })
            : poll({
                done: true,
                segments: [
                  { kind: "tool", id: "t1", name: "Read", detail: "/x", status: "done" } as never,
                ],
              }),
      },
      () => Promise.resolve({ entry: { id: "e1" } }),
      undefined,
      () => {
        listed++;
        return Promise.resolve({ entries: [] });
      },
    );
    await c.sendMessage("go");
    await flush();
    await c.sendMessage("go");
    await flush();
    expect(c.getState().turns.filter((t) => t.role === "error").length).toBe(1);
    expect(listed).toBe(0);
  });
});
