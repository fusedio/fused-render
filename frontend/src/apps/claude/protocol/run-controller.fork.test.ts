// "Fork from here": beginFork / cancelFork and the start call a fork makes.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, test } from "bun:test";

const { createChatController } = await import("./run-controller");
const { createMemoryParamsStore } = await import("../params/store");
const { AgentError } = await import("./agent");

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
  const CTX = "<breakout-context>\nThe user opened this side chat from 2 messages of their main chat.\n\nUser: q\n\nAssistant: a\n</breakout-context>";
  const started = {
    live_host: () => {
      throw new Error("a breakout must never probe a live host before its own session exists");
    },
    send: () => {
      throw new Error("a breakout must never follow up into another host");
    },
    start: () => ({ run_id: "rb", session_id: "brk", backend: "claude" }),
    poll: () => donePoll("brk"),
  };
  const fresh = (extra: Record<string, Handler> = {}, deps: Record<string, unknown> = {}) =>
    rig({ history: () => HISTORY, live_run: () => ({ run_id: "" }), ...started, ...extra }, { breakout: true, breakoutContext: CTX, ...deps });

  test("the first send starts a fresh session with breakout=1 and the context block", async () => {
    const r = fresh();
    await r.controller.sendMessage("what is this?");
    expect(r.of("live_host")).toHaveLength(0);
    expect(r.of("send")).toHaveLength(0);
    const f = r.of("start")[0]!.fields;
    expect(f).toMatchObject({ session_id: "", breakout: "1" });
    expect("fork_at" in f).toBe(false);
    expect(String(f.message)).toContain(CTX);
    expect(String(f.message)).toContain("what is this?");
    expect(r.controller.getState().sessionId).toBe("brk");
  });

  test("the first send spends no folder draft", async () => {
    const r = fresh();
    await r.controller.sendMessage("what is this?");
    expect("draft_key" in r.of("start")[0]!.fields).toBe(false);
  });

  test("the bubble shows what was typed; the context rides in raw", async () => {
    const r = fresh();
    await r.controller.sendMessage("what is this?");
    const t = r.controller.getState().turns.find((x) => x.role === "user")!;
    expect(t.text).toBe("what is this?");
    expect((t as { raw?: string }).raw).toContain(CTX);
  });

  test("opening loads no history and never attaches to a live run", async () => {
    const r = fresh({ live_run: () => ({ run_id: "parent-run" }) });
    expect(r.of("history")).toHaveLength(0);
    expect(r.of("live_run")).toHaveLength(0);
    expect(r.controller.getState().turns).toEqual([]);
  });

  test("a later send into the breakout's own session resumes it without the context", async () => {
    const r = fresh({ live_host: () => ({ run_id: "" }) });
    await r.controller.sendMessage("one");
    await r.controller.sendMessage("two");
    const starts = r.of("start").map((c) => c.fields);
    expect(starts).toHaveLength(2);
    expect(starts[1]).toMatchObject({ session_id: "brk", breakout: "resume" });
    expect(String(starts[1]!.message)).not.toContain("breakout-context");
  });

  test("a failed first start leaves the breakout unspent and resends the context", async () => {
    let n = 0;
    const r = fresh({ start: () => (n++ === 0 ? { error: "boom" } : { run_id: "rb", session_id: "brk" }) });
    await r.controller.sendMessage("one");
    await r.controller.sendMessage("two");
    const second = r.of("start")[1]!.fields;
    expect(second).toMatchObject({ breakout: "1", session_id: "" });
    expect(String(second.message)).toContain(CTX);
  });

  test("a first start that times out never adopts a live run in the file", async () => {
    const r = fresh({
      start: () => {
        throw new AgentError({ type: "Timeout", message: "start timed out after 60s" });
      },
      live_run: () => ({ run_id: "parent-run" }),
    });
    await r.controller.sendMessage("one");
    expect(r.of("live_run")).toHaveLength(0);
    expect(r.of("poll")).toHaveLength(0);
    expect(r.controller.getState().trouble).not.toBeNull();
  });

  test("a usage limit in a breakout never schedules a (policy-less) comeback", async () => {
    const posted: unknown[] = [];
    const r = fresh(
      {
        poll: () =>
          ({
            ...donePoll("brk"),
            error: "You've hit your session limit",
            quota: { status: "rejected", type: "five_hour", resets_at: 1789066800, utilization: null, windows: {} },
          }) as unknown as PollResponse,
      },
      {
        schedule: (b: unknown) => (posted.push(b), Promise.resolve({ entry: { id: "e" } })),
        listSchedule: () => Promise.resolve({ entries: [] }),
      },
    );
    await r.controller.sendMessage("go");
    await new Promise<void>((res) => setTimeout(res, 0));
    expect(posted).toEqual([]);
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

describe("allowDenied with a pending fork", () => {
  test("refuses before recording a grant, so the fork message is not consumed", async () => {
    const r = await opened({ allow_denied: () => ({ allowed: "t1", message: "run it again" }) });
    r.controller.beginFork(keyOf(r, "second"));
    await expect(r.controller.allowDenied("t1")).rejects.toThrow(
      "Send or cancel the pending fork before allowing this call.",
    );
    expect(r.of("allow_denied")).toHaveLength(0);
    expect(r.controller.getState().fork?.uuid).toBe("u2");
  });
});
