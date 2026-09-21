// The global model / thinking store's ORDERING rules (Bugbot on #1281,
// 2026-09-21): an answer from the server never lands on top of a pick made
// after it departed, and a refused write is taken back — unless a newer pick
// has already taken over.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();

import { afterEach, beforeEach, expect, test } from "bun:test";

const realFetch = globalThis.fetch;
type Pair = { model: string; effort: string };

/** A fetch stub whose answers are PROMISES THE TEST RESOLVES, so the order the
 *  network answers in is the order the test chooses. */
function stub() {
  const pending: { url: string; method: string; body: unknown; resolve: (r: Response) => void }[] = [];
  globalThis.fetch = ((url: string, init?: RequestInit) =>
    new Promise<Response>((resolve) => {
      pending.push({
        url: String(url),
        method: init?.method ?? "GET",
        body: init?.body ? JSON.parse(String(init.body)) : null,
        resolve,
      });
    })) as unknown as typeof fetch;
  const answer = (i: number, pair: Pair, status = 200) =>
    pending[i].resolve(new Response(JSON.stringify(pair), {
      status, headers: { "Content-Type": "application/json" },
    }));
  return { pending, answer };
}

const tick = () => new Promise((r) => setTimeout(r, 0));

let mod: typeof import("./claude-defaults");
beforeEach(async () => {
  mod = await import("./claude-defaults");
  mod.resetClaudeDefaultsForTests();
});
afterEach(() => {
  globalThis.fetch = realFetch;
});

test("a read that departed before a pick does not snap the pick back", async () => {
  const s = stub();
  const heard: Pair[] = [];
  mod.subscribeClaudeDefaults((d) => heard.push(d));
  const read = mod.readClaudeDefaults();          // GET #0 in flight
  void mod.setClaudeDefaults({ model: "opus" }); // pick while it is out: PUT #1
  expect(mod.getClaudeDefaults()?.model).toBe("opus");
  s.answer(0, { model: "fable", effort: "low" }); // the stale GET lands late
  expect(await read).toEqual({ model: "opus", effort: "" });
  expect(mod.getClaudeDefaults()?.model).toBe("opus");
  s.answer(1, { model: "opus", effort: "low" });
  await tick();
  expect(mod.getClaudeDefaults()).toEqual({ model: "opus", effort: "low" });
  expect(heard.some((d) => d.model === "fable")).toBe(false);
});

test("a refused write is taken back from the file", async () => {
  const s = stub();
  const heard: Pair[] = [];
  mod.subscribeClaudeDefaults((d) => heard.push(d));
  const write = mod.setClaudeDefaults({ model: "gpt-42" }); // PUT #0
  expect(mod.getClaudeDefaults()?.model).toBe("gpt-42");     // optimistic
  s.answer(0, { model: "", effort: "" }, 400);
  await tick();
  expect(s.pending[1].method).toBe("GET");                    // the re-read
  s.answer(1, { model: "fable", effort: "low" });
  expect(await write).toEqual({ model: "fable", effort: "low" });
  expect(heard[heard.length - 1]).toEqual({ model: "fable", effort: "low" });
});

test("a refused write's re-read does not clobber a newer pick", async () => {
  const s = stub();
  const first = mod.setClaudeDefaults({ model: "gpt-42" }); // PUT #0, will fail
  void mod.setClaudeDefaults({ effort: "high" });            // PUT #1, newer
  s.answer(0, { model: "", effort: "" }, 400);
  await tick();
  s.answer(2, { model: "fable", effort: "low" });            // GET #2: the file BEFORE #1 landed
  await first;
  // The stale re-read said nothing; the newer pick still speaks.
  expect(mod.getClaudeDefaults()?.effort).toBe("high");
  s.answer(1, { model: "fable", effort: "high" });
  await tick();
  expect(mod.getClaudeDefaults()).toEqual({ model: "fable", effort: "high" });
});

test("a write that finishes after a newer pick leaves the newer pick alone", async () => {
  const s = stub();
  void mod.setClaudeDefaults({ model: "opus" });  // PUT #0
  void mod.setClaudeDefaults({ model: "haiku" }); // PUT #1
  s.answer(0, { model: "opus", effort: "low" });  // #0 answers late
  await tick();
  expect(mod.getClaudeDefaults()?.model).toBe("haiku");
  s.answer(1, { model: "haiku", effort: "low" });
  await tick();
  expect(mod.getClaudeDefaults()).toEqual({ model: "haiku", effort: "low" });
});
