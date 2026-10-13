// The Share sheet's "Sign in to Fused" wait (`followCliLogin`): the CLI's
// browser login is followed on the events bus's `canvases.status` topic, not
// polled. The bus frames come from a scripted client installed with
// `setEventsClientForTests` — never `mock.module`, which is process-wide in
// bun (DownloadManager.test.tsx's header has the incident).
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// Dynamic, so the shim is in place before router.ts (pulled in transitively)
// reads `location` at module scope.
const { followCliLogin } = await import("@platform/ui/ShareAppModal");
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
  const resyncs: { topic: string; params: unknown }[] = [];
  setEventsClientForTests({
    subscribe: ((topic: string, params: unknown, cb: Frame) => {
      const sub: Sub = { topic, params, cb, open: true };
      subs.push(sub);
      return () => {
        sub.open = false;
      };
    }) as never,
    resync: ((topic: string, params: unknown) => {
      resyncs.push({ topic, params });
      return true;
    }) as never,
  });
  return {
    subs,
    resyncs,
    open: () => subs.filter((x) => x.open),
    push: (snap: unknown, meta: Record<string, unknown> = { gen: null }) => {
      for (const x of subs) if (x.open) x.cb(snap, null, meta);
    },
    fail: (error: string, status = 500) => {
      for (const x of subs) if (x.open) x.cb(null, null, { error, status });
    },
  };
}

afterEach(() => setEventsClientForTests(null));

const STATUS = { cli_found: true, logged_in: false, creds_stamp: 5, login_in_flight: true };

function watch(fromStamp: number | null) {
  const seen: string[] = [];
  const stop = followCliLogin(fromStamp, {
    completed: () => seen.push("completed"),
    abandoned: () => seen.push("abandoned"),
  });
  return { seen, stop };
}

test("subscribes to canvases.status and resyncs it right after the login POST", () => {
  const bus = fakeEvents();
  watch(5);
  expect(bus.open().map((s) => [s.topic, s.params])).toEqual([["canvases.status", {}]]);
  expect(bus.resyncs).toEqual([{ topic: "canvases.status", params: {} }]);
});

test("a new credentials stamp completes the login and closes the subscription", () => {
  const bus = fakeEvents();
  const { seen } = watch(5);
  bus.push(STATUS); // still in flight
  expect(seen).toEqual([]);
  // A re-login over a stale-but-present store: logged_in was already true,
  // the stamp moving is the whole signal.
  bus.push({ ...STATUS, logged_in: true, creds_stamp: 5 });
  expect(seen).toEqual([]);
  bus.push({ ...STATUS, logged_in: true, creds_stamp: 9 });
  expect(seen).toEqual(["completed"]);
  expect(bus.open()).toHaveLength(0);
  // Nothing after the verdict acts.
  bus.subs[0].cb({ ...STATUS, login_in_flight: false }, null, { gen: null });
  expect(seen).toEqual(["completed"]);
});

test("the child gone without a sign-in abandons the login and closes the subscription", () => {
  const bus = fakeEvents();
  const { seen } = watch(null);
  bus.push({ ...STATUS, creds_stamp: null, login_in_flight: false });
  expect(seen).toEqual(["abandoned"]);
  expect(bus.open()).toHaveLength(0);
});

test("a replayed snapshot is not a verdict — it predates the POST", () => {
  const bus = fakeEvents();
  const { seen } = watch(5);
  bus.push({ ...STATUS, login_in_flight: false }, { gen: null, replay: true });
  expect(seen).toEqual([]);
  expect(bus.open()).toHaveLength(1);
});

test("a refused frame is what a failed status GET was: nothing", () => {
  const bus = fakeEvents();
  const { seen } = watch(5);
  bus.fail("internal error");
  expect(seen).toEqual([]);
  expect(bus.open()).toHaveLength(1);
  bus.push({ ...STATUS, logged_in: true, creds_stamp: 6 });
  expect(seen).toEqual(["completed"]);
});

test("the disposer (unmount) closes the subscription and silences it", () => {
  const bus = fakeEvents();
  const { seen, stop } = watch(5);
  stop();
  expect(bus.open()).toHaveLength(0);
  bus.subs[0].cb({ ...STATUS, logged_in: true, creds_stamp: 6 }, null, { gen: null });
  expect(seen).toEqual([]);
  stop(); // idempotent
});

test("no timer re-asks /api/canvases/status any more", () => {
  const src = readFileSync(join(import.meta.dir, "ShareAppModal.tsx"), "utf8");
  expect(src).not.toMatch(/setInterval|LOGIN_POLL_MS|clearInterval/);
  expect(src).not.toContain('"/api/canvases/status"');
  expect(src).toContain('subscribeTopic<CanvasesStatusLite>("canvases.status"');
});
