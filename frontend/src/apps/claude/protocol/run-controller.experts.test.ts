// EXPERTS (docs/experts.md): `extraStartParams` rides on every
// start and send of an expert session, and a chat without it is unchanged.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, test } from "bun:test";

const { createChatController } = await import("./run-controller");
const { createMemoryParamsStore } = await import("../params/store");

import type { runAgent } from "./agent";
import type { PollResponse } from "./types";

type Handler = (fields: Record<string, unknown>) => unknown;

function rig(handlers: Record<string, Handler>, deps: Record<string, unknown> = {}, seed: Record<string, string> = {}) {
  const calls: { action: string; fields: Record<string, unknown> }[] = [];
  const run = ((action: string, fields: Record<string, unknown>) => {
    calls.push({ action, fields });
    const h = handlers[action];
    if (!h) throw new Error("no handler for " + action);
    return Promise.resolve(h(fields));
  }) as unknown as typeof runAgent;
  const controller = createChatController({
    file: "/w/experts/canvas",
    params: createMemoryParamsStore(seed),
    run,
    sleep: () => Promise.resolve(),
    now: () => 1_000,
    hasPane: () => false,
    ...deps,
  });
  return { controller, of: (a: string) => calls.filter((c) => c.action === a) };
}

const donePoll = (sid: string) =>
  ({
    text: "ok", done: true, session_id: sid, error: "", tokens: 0, phase: "composing",
    message: "", permissions: [], app_state: [], mode: "prompt", skills: [], retry: null,
    retry_total: 0, retry_status: 0, cancelled: false, tasks_pending: false,
    activity: { tool: null, tools_open: 0, hook: "", tool_input_bytes: 0, tasks: [] },
    segments: [{ kind: "text", id: "ok", text: "ok" }],
  }) as unknown as PollResponse;

const SPEC = { expert: "c4f1a2b3:main" };

describe("extraStartParams", () => {
  test("a fresh expert chat's first start carries the expert field", async () => {
    const r = rig(
      { start: () => ({ run_id: "r1", session_id: "s1", backend: "claude" }), poll: () => donePoll("s1") },
      { extraStartParams: SPEC },
    );
    await r.controller.sendMessage("hello");
    expect(r.of("start")[0]!.fields).toMatchObject({ expert: "c4f1a2b3:main", session_id: "" });
  });

  test("a send into a live host of the session carries it too", async () => {
    const r = rig(
      {
        history: () => ({ turns: [] }),
        live_run: () => ({ run_id: "" }),
        live_host: () => ({ run_id: "host" }),
        send: () => ({ sent: true }),
        poll: () => donePoll("s1"),
      },
      { extraStartParams: SPEC },
      { session_id: "s1" },
    );
    await r.controller.sendMessage("again");
    expect(r.of("send")[0]!.fields).toMatchObject({ expert: "c4f1a2b3:main", run_id: "host" });
  });

  test("a chat without it sends no expert field", async () => {
    const r = rig({ start: () => ({ run_id: "r1", session_id: "s1" }), poll: () => donePoll("s1") });
    await r.controller.sendMessage("hello");
    expect("expert" in r.of("start")[0]!.fields).toBe(false);
  });
});
