// PR status cache (lib/prStatus.ts): batching, in-flight dedupe, refresh rules.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, describe, expect, it } from "bun:test";

import { peekPrStatus, requestPrStatuses, resetPrStatusForTests } from "./prStatus";

type Call = { url: string; refs: string[] };
let calls: Call[];
let answers: Record<string, string | null>;
let realFetch: typeof fetch;
let realNow: () => number;
let clock: number;

beforeEach(() => {
  resetPrStatusForTests();
  calls = [];
  answers = {};
  clock = 1_000_000;
  realFetch = globalThis.fetch;
  realNow = Date.now;
  Date.now = () => clock;
  globalThis.fetch = (async (url: string, init: { body: string }) => {
    const refs = JSON.parse(init.body).refs as string[];
    calls.push({ url, refs });
    const out: Record<string, string | null> = {};
    for (const r of refs) out[r] = answers[r] ?? null;
    return { ok: true, status: 200, json: async () => out } as Response;
  }) as unknown as typeof fetch;
});
afterEach(() => {
  globalThis.fetch = realFetch;
  Date.now = realNow;
});

describe("prStatus", () => {
  it("asks once for a batch and caches the answers", async () => {
    answers = { "a/b#1": "open", "a/b#2": "merged" };
    await requestPrStatuses(["a/b#1", "a/b#2"]);
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("/api/tasks/pr-status");
    expect(peekPrStatus("a/b#1")).toBe("open");
    expect(peekPrStatus("a/b#2")).toBe("merged");
    expect(peekPrStatus("a/b#3")).toBeUndefined();
  });
  it("dedupes keys and skips unknown-repo keys", async () => {
    await requestPrStatuses(["a/b#1", "a/b#1", "?#5"]);
    expect(calls[0].refs).toEqual(["a/b#1"]);
    await requestPrStatuses(["?#5"]);
    expect(calls).toHaveLength(1);
  });
  it("shares an in-flight request", async () => {
    const one = requestPrStatuses(["a/b#1"]);
    const two = requestPrStatuses(["a/b#1"]);
    await Promise.all([one, two]);
    expect(calls).toHaveLength(1);
  });
  it("does not re-ask while fresh, re-asks an open PR after 120 s, never a merged one", async () => {
    answers = { "a/b#1": "open", "a/b#2": "merged" };
    await requestPrStatuses(["a/b#1", "a/b#2"]);
    clock += 119_000;
    await requestPrStatuses(["a/b#1", "a/b#2"]);
    expect(calls).toHaveLength(1);
    clock += 2_000;
    await requestPrStatuses(["a/b#1", "a/b#2"]);
    expect(calls).toHaveLength(2);
    expect(calls[1].refs).toEqual(["a/b#1"]);
  });
  it("a failed request leaves the keys unknown and does not throw", async () => {
    globalThis.fetch = (async () => {
      throw new Error("offline");
    }) as unknown as typeof fetch;
    await requestPrStatuses(["a/b#1"]);
    expect(peekPrStatus("a/b#1")).toBeNull();
  });
  it("splits more than 100 keys into several requests", async () => {
    const keys = Array.from({ length: 150 }, (_, i) => `a/b#${i + 1}`);
    await requestPrStatuses(keys);
    expect(calls.map((c) => c.refs.length)).toEqual([100, 50]);
  });
});
