// The canvas workspace's sync status follows the events bus's
// `canvases.sync {name}` topic for as long as the workspace is mounted — no
// 2 s status poll. `followCanvasSync` is the whole of that wiring; the
// scripted client is installed with `setEventsClientForTests` (never
// `mock.module`, which is process-wide in bun).
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const { followCanvasSync } = await import("./CanvasWorkspace");
const { setEventsClientForTests } = await import("@platform/lib/events");

type Frame = (snap: unknown, delta: unknown, meta: Record<string, unknown>) => void;
interface Sub {
  topic: string;
  params: unknown;
  cb: Frame;
  open: boolean;
}

function fakeEvents() {
  const subs: Sub[] = [];
  const state = new Set<(connected: boolean) => void>();
  setEventsClientForTests({
    subscribe: ((topic: string, params: unknown, cb: Frame) => {
      const sub: Sub = { topic, params, cb, open: true };
      subs.push(sub);
      return () => {
        sub.open = false;
      };
    }) as never,
    resync: () => true,
    onState: ((fn: (connected: boolean) => void) => {
      state.add(fn);
      return () => state.delete(fn);
    }) as never,
  });
  return {
    subs,
    state,
    open: () => subs.filter((x) => x.open),
    push: (snap: unknown) => {
      for (const x of subs) if (x.open) x.cb(snap, null, { gen: null });
    },
  };
}

afterEach(() => setEventsClientForTests(null));

const SYNC = {
  name: "demo",
  watching: true,
  push_state: "idle",
  push_seq: 1,
  last_push_at: null,
  pull_seq: 0,
  last_pull_at: null,
  error: null,
  error_detail: [],
  dir: "/tmp/canvases/demo",
  fix_active: false,
  agent_active: false,
  pulling: false,
};

function follow() {
  const seen: unknown[] = [];
  let lost = 0;
  const stop = followCanvasSync("demo", {
    status: (s) => seen.push(s),
    lost: () => {
      lost += 1;
    },
  });
  return { seen, lost: () => lost, stop };
}

test("subscribes to canvases.sync for the canvas and hands every snapshot over", () => {
  const bus = fakeEvents();
  const { seen } = follow();
  expect(bus.open().map((s) => [s.topic, s.params])).toEqual([["canvases.sync", { name: "demo" }]]);
  bus.push(SYNC);
  bus.push({ ...SYNC, push_state: "pushing" });
  expect(seen).toEqual([SYNC, { ...SYNC, push_state: "pushing" }]);
});

test("a refused frame or the socket going away is `lost` (finding 6: no lock stranded ON)", () => {
  const bus = fakeEvents();
  const { lost, seen } = follow();
  for (const x of bus.open()) x.cb(null, null, { error: "internal error", status: 500 });
  expect(lost()).toBe(1);
  expect(seen).toEqual([]);
  for (const fn of bus.state) fn(true); // a reconnect is not a loss
  expect(lost()).toBe(1);
  for (const fn of bus.state) fn(false);
  expect(lost()).toBe(2);
});

test("the unmount closes the subscription and the socket-state listener", () => {
  const bus = fakeEvents();
  const { stop } = follow();
  expect(bus.state.size).toBe(1);
  stop();
  expect(bus.open()).toHaveLength(0);
  expect(bus.state.size).toBe(0);
});

test("no timer re-asks the sync status any more", () => {
  const src = readFileSync(join(import.meta.dir, "CanvasWorkspace.tsx"), "utf8");
  expect(src).not.toMatch(/setInterval|SYNC_POLL_MS|FAILED_POLLS_BEFORE_RELEASE|getSyncStatus/);
  expect(src).toContain('subscribeTopic<SyncStatus>("canvases.sync", { name }');
  // The resync after this page's own fix POST, never from a timer.
  expect(src).toContain('resyncTopic("canvases.sync", { name })');
});
