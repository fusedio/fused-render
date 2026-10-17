// Browser-side queue-time collector (netTiming.ts).
import { expect, test } from "bun:test";

import { ROUTE_CASES } from "./netTiming.cases";
import {
  FLUSH_MS,
  NetCollector,
  hasWatch,
  normalizeRoute,
  queueMs,
  serverMs,
  type TimingLike,
} from "./netTiming";

test("normalizeRoute matches the server's rule", () => {
  for (const [input, want] of ROUTE_CASES) expect(normalizeRoute(input)).toBe(want);
});

const entry = (over: Partial<TimingLike> = {}): TimingLike => ({
  name: "http://127.0.0.1:1777/api/fs/list?path=/a",
  startTime: 100,
  requestStart: 400,
  duration: 450,
  serverTiming: [
    { name: "fr", duration: 12 },
    { name: "fr-watch", duration: 0 },
  ],
  ...over,
});

test("queueMs is requestStart - startTime, with fallbacks", () => {
  expect(queueMs(entry())).toBe(300);
  expect(queueMs(entry({ requestStart: 0, connectStart: 130 }))).toBe(30);
  expect(queueMs(entry({ requestStart: 0, connectStart: 0, fetchStart: 0 }))).toBe(0);
  expect(queueMs(entry({ requestStart: 50 }))).toBe(0);
});

test("serverMs and hasWatch read Server-Timing", () => {
  expect(serverMs(entry())).toBe(12);
  expect(hasWatch(entry())).toBe(true);
  expect(hasWatch(entry({ serverTiming: [{ name: "fr", duration: 1 }] }))).toBe(false);
  expect(serverMs(entry({ serverTiming: [] }))).toBeNull();
});

function harness() {
  let t = 100_000;
  const sent: any[] = [];
  const timers: { fn: () => void; ms: number }[] = [];
  const c = new NetCollector({
    page: () => "/monitor",
    now: () => t,
    send: (j) => sent.push(JSON.parse(j)),
    origin: "http://127.0.0.1:1777",
    setTimer: (fn, ms) => {
      timers.push({ fn, ms });
      return timers.length;
    },
  });
  return { c, sent, timers, advance: (ms: number) => (t += ms) };
}

test("aggregates per route and flushes one beacon", () => {
  const h = harness();
  h.c.add(entry());
  h.c.add(entry({ requestStart: 150 }));
  h.c.add(entry({ name: "http://127.0.0.1:1777/api/jobs/987654321" }));
  expect(h.timers).toHaveLength(1);
  h.timers[0].fn();
  expect(h.sent).toHaveLength(1);
  const p = h.sent[0];
  expect(p.page).toBe("/monitor");
  expect(p.routes["/api/fs/list"]).toMatchObject({ count: 2, sum: 350, max: 300 });
  expect(p.routes["/api/jobs/{id}"].count).toBe(1);
});

test("ignores other origins, non-api, the beacon itself and unwatched entries", () => {
  const h = harness();
  h.c.add(entry({ name: "http://example.com/api/x" }));
  h.c.add(entry({ name: "http://127.0.0.1:1777/static/a.js" }));
  h.c.add(entry({ name: "http://127.0.0.1:1777/api/system/net/client" }));
  h.c.add(entry({ serverTiming: [{ name: "fr", duration: 3 }] }));
  expect(h.timers).toHaveLength(0);
  expect(h.c.flush()).toBe(false);
  expect(h.sent).toHaveLength(0);
});

test("sends at most one beacon per 10 s and nothing without new entries", () => {
  const h = harness();
  h.c.add(entry());
  h.timers[0].fn(); // the collector's own timer
  expect(h.sent).toHaveLength(1);
  expect(h.c.flush()).toBe(false); // nothing new
  h.advance(2_000);
  h.c.add(entry());
  expect(h.c.flush()).toBe(false); // too soon
  // the timer scheduled for the remainder fires and sends
  const t = h.timers[h.timers.length - 1];
  expect(t.ms).toBeGreaterThan(7_000);
  expect(t.ms).toBeLessThanOrEqual(FLUSH_MS);
  h.advance(t.ms);
  t.fn();
  expect(h.sent).toHaveLength(2);
});

test("nothing is sent once the watch is over 15 s old", () => {
  const h = harness();
  h.c.add(entry());
  h.advance(16_000);
  expect(h.c.flush()).toBe(false);
  expect(h.sent).toHaveLength(0);
  // and the stale batch is dropped, not sent when the next entry arrives
  h.c.add(entry({ requestStart: 110 }));
  h.advance(FLUSH_MS);
  h.c.flush();
  expect(h.sent[0].routes["/api/fs/list"].count).toBe(1);
});

test("samples are capped", () => {
  const h = harness();
  for (let i = 0; i < 100; i++) h.c.add(entry({ requestStart: 100 + i }));
  h.c.flush();
  expect(h.sent[0].routes["/api/fs/list"].samples).toHaveLength(64);
  expect(h.sent[0].routes["/api/fs/list"].count).toBe(100);
});
