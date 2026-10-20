import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

const { useRemoteControl } = await import("./RemoteControl");

interface Pending {
  action: string;
  resolve(body: unknown): void;
  reject(err: Error): void;
}

const realFetch = globalThis.fetch;
let pending: Pending[] = [];
let latest: ReturnType<typeof useRemoteControl>;
let root: ReactTestRenderer;

function Probe() {
  latest = useRemoteControl("s1", "/tmp/chat.jsonl");
  return null;
}

function take(action: string): Pending {
  const i = pending.findIndex((p) => p.action === action);
  return pending.splice(i, 1)[0]!;
}

async function settle(p: Pending, body: unknown) {
  await act(async () => {
    p.resolve(body);
  });
}

beforeEach(() => {
  pending = [];
  (globalThis as { fetch: unknown }).fetch = (_url: unknown, init?: { body?: unknown }) =>
    new Promise((resolve, reject) => {
      const { action } = JSON.parse(String(init?.body)) as { action: string };
      pending.push({
        action,
        resolve: (body) => resolve({ ok: true, status: 200, json: async () => body }),
        reject,
      });
    });
});

afterEach(() => {
  act(() => root.unmount());
  (globalThis as { fetch: unknown }).fetch = realFetch;
});

test("a status reply that predates a start is dropped", async () => {
  await act(async () => {
    root = create(createElement(Probe));
  });
  const status = take("status");
  act(() => latest.ask(""));
  act(() => latest.start());
  await settle(take("start"), { state: "starting", url: "", terminal_id: "t" });
  expect(latest.view.state).toBe("starting");
  await settle(status, { state: "off" });
  expect(latest.view.state).toBe("starting");
});

test("a failed stop keeps the Remote Control block", async () => {
  await act(async () => {
    root = create(createElement(Probe));
  });
  await settle(take("status"), { state: "active", url: "https://claude.ai/code/session_X", terminal_id: "t" });
  expect(latest.view.state).toBe("active");

  act(() => latest.stop());
  await settle(take("stop"), { error: "boom" });
  expect(latest.view.state).toBe("active");

  act(() => latest.stop());
  await act(async () => {
    take("stop").reject(new Error("network"));
  });
  expect(latest.view.state).toBe("active");

  act(() => latest.stop());
  await settle(take("stop"), { state: "off" });
  expect(latest.view.state).toBe("off");
});
