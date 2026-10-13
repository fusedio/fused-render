// A build's task handle settles off the events bus (`tasks.listing`), and its
// second look at a quiet "done" row is the row the feed still holds — never a
// timer that re-reads `/api/tasks`. Frames come from a scripted client
// installed with `setEventsClientForTests` (never `mock.module`, which is
// process-wide in bun); the create POST from a `fetch` stub.
import { afterEach, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { setEventsClientForTests } from "@platform/lib/events";
import { tasksCreate } from "./tasks";

type Frame = (snap: unknown, delta: unknown, meta: Record<string, unknown>) => void;
interface Sub {
  topic: string;
  params: unknown;
  cb: Frame;
  open: boolean;
}

function fakeEvents() {
  const subs: Sub[] = [];
  setEventsClientForTests({
    subscribe: ((topic: string, params: unknown, cb: Frame) => {
      const sub: Sub = { topic, params, cb, open: true };
      subs.push(sub);
      return () => {
        sub.open = false;
      };
    }) as never,
    resync: () => true,
  });
  return {
    subs,
    open: () => subs.filter((x) => x.open),
    push: (snap: unknown) => {
      for (const x of subs) if (x.open) x.cb(snap, null, { gen: null });
    },
  };
}

const realFetch = globalThis.fetch;
let hits: string[] = [];
afterEach(() => {
  globalThis.fetch = realFetch;
  setEventsClientForTests(null);
});

function stubCreate() {
  hits = [];
  globalThis.fetch = (async (url: string) => {
    hits.push(url);
    const body = url === "/api/tasks/create" ? { key: "pending:e1", entry_id: "e1" } : { error: "unmocked" };
    return new Response(JSON.stringify(body), { status: url === "/api/tasks/create" ? 200 : 404 });
  }) as typeof fetch;
}

test("a handle rides tasks.listing and settles on done twice in a row, then lets go", async () => {
  stubCreate();
  const bus = fakeEvents();
  const handle = await tasksCreate({ prompt: "build it" });
  expect(bus.open().map((s) => [s.topic, s.params])).toEqual([["tasks.listing", {}]]);
  let settled: unknown = null;
  void handle.done.then((row) => (settled = row));

  bus.push({ tasks: [{ key: "sess-1", entry_id: "e1", status: "in_progress" }] });
  expect(handle.key).toBe("sess-1");
  // A refused frame proves nothing either way.
  for (const x of bus.open()) x.cb(null, null, { error: "internal error", status: 500 });
  bus.push({ tasks: [{ key: "sess-1", entry_id: "e1", status: "done" }] });
  await Promise.resolve();
  expect(settled).toBeNull(); // one quiet look is not enough: status flickers
  bus.push({ tasks: [{ key: "sess-1", entry_id: "e1", status: "done", title: "t" }] });
  await Promise.resolve();
  expect((settled as { status: string } | null)?.status).toBe("done");
  expect(bus.open()).toHaveLength(0);
  // The create POST and nothing else: no listing re-read.
  expect(hits).toEqual(["/api/tasks/create"]);
});

test("the quiet-period second look reads the held feed row, never the network", () => {
  const src = readFileSync(join(import.meta.dir, "tasks.ts"), "utf8");
  const arm = src.slice(src.indexOf("function arm()"), src.indexOf("stop = subscribe(scope"));
  expect(arm).toContain("setTimeout(");
  expect(arm).toContain("feedRows(f).find(mine)");
  expect(arm).not.toMatch(/listing\(|taskFetch|fetch\(/);
  // …and nothing re-arms on a failure: there is no failure to retry.
  expect(arm.match(/arm\(\)/g)?.length).toBe(1);
});
