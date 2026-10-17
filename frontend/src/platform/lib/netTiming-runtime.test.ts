// The runtime.js copy of the queue-time collector, executed. runtime.js has no
// bundler, so netTiming.ts's logic is repeated there between `<net-timing>`
// markers; this extracts that block and runs the same route fixtures and the
// same watch/flush rules against it.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { ROUTE_CASES } from "./netTiming.cases";

const SRC = readFileSync(
  join(import.meta.dir, "../../../../fused_render/static/runtime.js"),
  "utf8"
);
const block = SRC.split("// <net-timing>")[1].split("// </net-timing>")[0];
const api = new Function(
  `${block}; return {netNormalizeRoute, netQueueMs, netCreateCollector};`
)() as any;

test("runtime.js route rule matches the server's", () => {
  for (const [input, want] of ROUTE_CASES) expect(api.netNormalizeRoute(input)).toBe(want);
});

test("runtime.js queue time", () => {
  expect(api.netQueueMs({ startTime: 100, requestStart: 400 })).toBe(300);
  expect(api.netQueueMs({ startTime: 100, requestStart: 0, connectStart: 130 })).toBe(30);
  expect(api.netQueueMs({ startTime: 100, requestStart: 50 })).toBe(0);
});

function harness() {
  let t = 100_000;
  const sent: any[] = [];
  const timers: (() => void)[] = [];
  const c = api.netCreateCollector({
    page: () => "%2Fapps%2Fa.html",
    now: () => t,
    send: (j: string) => sent.push(JSON.parse(j)),
    origin: "http://127.0.0.1:1777",
    setTimer: (fn: () => void) => timers.push(fn),
  });
  return { c, sent, timers, advance: (ms: number) => (t += ms) };
}
const E = (over: any = {}) => ({
  name: "http://127.0.0.1:1777/api/fs/list?path=/a",
  startTime: 100,
  requestStart: 400,
  serverTiming: [{ name: "fr-watch" }],
  ...over,
});

test("runtime.js collector aggregates and flushes under the watch rules", () => {
  const h = harness();
  h.c.add(E());
  h.c.add(E({ requestStart: 150 }));
  h.c.add(E({ name: "http://127.0.0.1:1777/api/system/net/client" }));
  h.c.add(E({ name: "http://other/api/x" }));
  h.c.add(E({ serverTiming: [] }));
  h.timers[0]();
  expect(h.sent).toHaveLength(1);
  expect(h.sent[0].page).toBe("%2Fapps%2Fa.html");
  expect(h.sent[0].routes["/api/fs/list"]).toMatchObject({ count: 2, sum: 350, max: 300 });
  expect(Object.keys(h.sent[0].routes)).toHaveLength(1);
  h.advance(2_000);
  h.c.add(E());
  expect(h.c.flush()).toBe(false); // under 10 s since the last beacon
  h.advance(20_000);
  expect(h.c.flush()).toBe(false); // watch is stale: dropped, nothing sent
  expect(h.sent).toHaveLength(1);
});
