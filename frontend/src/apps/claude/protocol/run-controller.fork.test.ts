// "Fork from here": beginFork / cancelFork and the start call a fork makes.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, test } from "bun:test";

const { createChatController } = await import("./run-controller");
const { createMemoryParamsStore } = await import("../params/store");

import type { runAgent } from "./agent";
import type { PollResponse } from "./types";

type Handler = (fields: Record<string, unknown>, call: number) => unknown;

function rig(handlers: Record<string, Handler>, deps: Record<string, unknown> = {}) {
  const calls: { action: string; fields: Record<string, unknown> }[] = [];
  const run = ((action: string, fields: Record<string, unknown>) => {
    calls.push({ action, fields });
    const h = handlers[action];
    if (!h) throw new Error("no handler for " + action);
    return Promise.resolve(h(fields, 0));
  }) as unknown as typeof runAgent;
  const controller = createChatController({
    file: "/proj/app.py",
    params: createMemoryParamsStore(),
    run,
    sleep: () => Promise.resolve(),
    now: () => 1_000,
    hasPane: () => true,
    ...deps,
  });
  return { controller, calls, of: (a: string) => calls.filter((c) => c.action === a) };
}

const donePoll = (sid: string) =>
  ({
    text: "ok", done: true, session_id: sid, error: "", tokens: 0, phase: "composing",
    message: "", permissions: [], app_state: [], mode: "prompt", skills: [], retry: null,
    retry_total: 0, retry_status: 0, cancelled: false, tasks_pending: false,
    activity: { tool: null, tools_open: 0, hook: "", tool_input_bytes: 0, tasks: [] },
    segments: [{ kind: "text", id: "ok", text: "ok" }],
  }) as unknown as PollResponse;

const HISTORY = {
  turns: [
    { role: "user" as const, text: "first", uuid: "u1" },
    { role: "assistant" as const, text: "a1", uuid: "a1" },
    { role: "user" as const, text: "second", uuid: "u2" },
    { role: "assistant" as const, text: "a2", uuid: "a2" },
  ],
};

async function opened(
  extra: Record<string, Handler> = {},
  backend = "claude",
  deps: Record<string, unknown> = {},
) {
  const r = rig(
    {
      history: () => HISTORY,
      live_run: () => ({ run_id: "" }),
      ...extra,
    },
    deps,
  );
  await r.controller.openSession("old", { backend });
  return r;
}

const keyOf = (r: Awaited<ReturnType<typeof opened>>, text: string) =>
  r.controller.getState().turns.find((t) => t.role === "user" && t.text === text)!.key;

describe("beginFork / cancelFork", () => {
  test("hides the turn and everything after, returns its text", async () => {
    const r = await opened();
    const text = r.controller.beginFork(keyOf(r, "second"));
    expect(text).toBe("second");
    const s = r.controller.getState();
    expect(s.turns.map((t) => t.role === "user" && t.text)).toEqual(["first", false]);
    expect(s.fork?.uuid).toBe("u2");
    expect(s.fork?.hidden.length).toBe(2);
  });

  test("cancel restores the hidden turns and clears the fork", async () => {
    const r = await opened();
    r.controller.beginFork(keyOf(r, "second"));
    r.controller.cancelFork();
    const s = r.controller.getState();
    expect(s.turns.length).toBe(4);
    expect(s.fork).toBe(null);
  });

  test("a second, earlier fork then cancel restores the original order", async () => {
    const r = await opened();
    const before = r.controller.getState().turns.map((t) => t.key);
    r.controller.beginFork(keyOf(r, "second"));
    r.controller.beginFork(keyOf(r, "first"));
    expect(r.controller.getState().turns.length).toBe(0);
    r.controller.cancelFork();
    expect(r.controller.getState().turns.map((t) => t.key)).toEqual(before);
  });

  test("refused for a Copilot chat, an unknown key, and a turn with no uuid", async () => {
    const cp = await opened({}, "copilot");
    expect(cp.controller.beginFork(keyOf(cp, "second"))).toBe(null);
    expect(cp.controller.getState().turns.length).toBe(4);
    const r = await opened();
    expect(r.controller.beginFork("nope")).toBe(null);
    const asst = r.controller.getState().turns.find((t) => t.role === "assistant")!;
    expect(r.controller.beginFork(asst.key)).toBe(null);
  });

  test("a new session switch clears the fork", async () => {
    const r = await opened();
    r.controller.beginFork(keyOf(r, "second"));
    await r.controller.openSession("other", { backend: "claude" });
    expect(r.controller.getState().fork).toBe(null);
  });
});

describe("sendMessage with a fork", () => {
  test("starts a new session with fork_at, never probes the old live host", async () => {
    const r = await opened({
      live_host: () => ({ run_id: "r-old" }),
      send: () => {
        throw new Error("must not follow up into the old host");
      },
      start: () => ({ run_id: "r2", session_id: "new-sid", backend: "claude" }),
      poll: () => donePoll("new-sid"),
    });
    r.controller.beginFork(keyOf(r, "second"));
    await r.controller.sendMessage("edited second");
    expect(r.of("send")).toHaveLength(0);
    expect(r.of("live_host")).toHaveLength(0);
    expect(r.of("start")[0]!.fields).toMatchObject({
      session_id: "old",
      fork_at: "u2",
    });
    const s = r.controller.getState();
    expect(s.fork).toBe(null);
    expect(s.sessionId).toBe("new-sid");
    expect(s.turns.some((t) => t.role === "user" && t.text === "edited second")).toBe(true);
    expect(s.turns.some((t) => t.role === "user" && t.text === "second")).toBe(false);
  });

  test("a start answering fork_clamped sets state.forkClamped", async () => {
    const r = await opened({
      start: () => ({ run_id: "r2", session_id: "new-sid", backend: "claude", fork_clamped: true }),
      poll: () => donePoll("new-sid"),
    });
    r.controller.beginFork(keyOf(r, "second"));
    await r.controller.sendMessage("edited second");
    expect(r.controller.getState().forkClamped).toBe(true);
  });

  test("forkClamped stays falsy when the start does not carry the flag", async () => {
    const r = await opened({
      start: () => ({ run_id: "r2", session_id: "new-sid", backend: "claude" }),
      poll: () => donePoll("new-sid"),
    });
    r.controller.beginFork(keyOf(r, "second"));
    await r.controller.sendMessage("edited second");
    expect(r.controller.getState().forkClamped).toBeFalsy();
  });

  test("a refused start keeps the fork set", async () => {
    const r = await opened({
      start: () => ({ error: "message not found in this conversation" }),
    });
    r.controller.beginFork(keyOf(r, "second"));
    await r.controller.sendMessage("edited");
    expect(r.controller.getState().fork?.uuid).toBe("u2");
  });

  test("a plain send carries no fork_at", async () => {
    const r = await opened({
      live_host: () => ({ run_id: "" }),
      start: () => ({ run_id: "r2", session_id: "old" }),
      poll: () => donePoll("old"),
    });
    await r.controller.sendMessage("more");
    expect("fork_at" in r.of("start")[0]!.fields).toBe(false);
  });
});

describe("breakout posture", () => {
  const started = {
    live_host: () => {
      throw new Error("a breakout must never probe the parent's live host");
    },
    send: () => {
      throw new Error("a breakout must never follow up into the parent's host");
    },
    start: () => ({ run_id: "rb", session_id: "brk", backend: "claude" }),
    poll: () => donePoll("brk"),
  };

  test("the first send forks at the chosen message with breakout=1", async () => {
    const r = await opened(started, "claude", { breakout: true, breakoutAt: "u2" });
    await r.controller.sendMessage("what is this?");
    expect(r.of("live_host")).toHaveLength(0);
    expect(r.of("send")).toHaveLength(0);
    expect(r.of("start")[0]!.fields).toMatchObject({
      session_id: "old",
      fork_at: "u2",
      breakout: "1",
    });
    expect(r.controller.getState().sessionId).toBe("brk");
  });

  test("opening hides the chosen message and what follows, and never attaches to a live run", async () => {
    const r = await opened(
      { live_run: () => ({ run_id: "parent-run" }), live_host: () => ({ run_id: "parent-run" }) },
      "claude",
      { breakout: true, breakoutAt: "u2" },
    );
    expect(
      r.controller.getState().turns.map((t) => t.role === "user" && t.text),
    ).toEqual(["first", false]);
    expect(r.of("live_run")).toHaveLength(0);
  });

  test("a tip breakout sends an empty fork_at and keeps every turn", async () => {
    const r = await opened(started, "claude", { breakout: true });
    expect(r.controller.getState().turns.length).toBe(4);
    await r.controller.sendMessage("summarize");
    const f = r.of("start")[0]!.fields;
    expect(f.breakout).toBe("1");
    expect("fork_at" in f).toBe(false);
  });

  test("a later send into the breakout's own session resumes it, never forks again", async () => {
    const r = await opened(
      {
        ...started,
        live_host: () => ({ run_id: "" }),
        start: (f) =>
          f.breakout === "1"
            ? { run_id: "rb", session_id: "brk", backend: "claude" }
            : { run_id: "rb2", session_id: "brk", backend: "claude" },
      },
      "claude",
      { breakout: true, breakoutAt: "u2" },
    );
    await r.controller.sendMessage("one");
    await r.controller.sendMessage("two");
    const starts = r.of("start").map((c) => c.fields);
    expect(starts).toHaveLength(2);
    expect(starts[1]).toMatchObject({ session_id: "brk", breakout: "resume" });
    expect("fork_at" in starts[1]!).toBe(false);
  });

  test("breakoutSends counts every send in breakout posture", async () => {
    const r = await opened(
      { ...started, live_host: () => ({ run_id: "" }) },
      "claude",
      { breakout: true, breakoutAt: "u2" },
    );
    expect(r.controller.getState().breakoutSends).toBe(0);
    await r.controller.sendMessage("one");
    expect(r.controller.getState().breakoutSends).toBe(1);
    await r.controller.sendMessage("two");
    expect(r.controller.getState().breakoutSends).toBe(2);
  });

  test("a plain chat never counts breakout sends", async () => {
    const r = await opened({ start: () => ({ run_id: "r", session_id: "old" }), poll: () => donePoll("old") });
    await r.controller.sendMessage("x");
    expect(r.controller.getState().breakoutSends).toBe(0);
  });

  test("a failed first start leaves the breakout unspent", async () => {
    let n = 0;
    const r = await opened(
      {
        ...started,
        start: () => (n++ === 0 ? { error: "boom" } : { run_id: "rb", session_id: "brk" }),
      },
      "claude",
      { breakout: true, breakoutAt: "u2" },
    );
    await r.controller.sendMessage("one");
    await r.controller.sendMessage("two");
    expect(r.of("start")[1]!.fields).toMatchObject({ breakout: "1", fork_at: "u2" });
  });

  test("a plain chat sends no breakout field", async () => {
    const r = await opened({
      live_host: () => ({ run_id: "" }),
      start: () => ({ run_id: "r2", session_id: "old" }),
      poll: () => donePoll("old"),
    });
    await r.controller.sendMessage("more");
    expect("breakout" in r.of("start")[0]!.fields).toBe(false);
  });
});
