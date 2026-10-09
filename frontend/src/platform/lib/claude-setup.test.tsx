// useClaudeSetup's install / sign-in follow, driven through the events bus.
//
// The machine's rules are pinned against the source in claude-health.test.ts;
// this suite drives the half that moved off a timer: while an install runs (or
// a sign-in is open) the hook follows `claude.setup` and applies its `.install`
// (`.login`) record, re-probes health when it ends, and lets go of the
// subscription. Frames come from a scripted events client installed with
// `setEventsClientForTests`; HTTP is a route-keyed fetch stub — never
// `mock.module`, which is process-wide in bun.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create } from "react-test-renderer";
import type { ClaudeSetup } from "@platform/lib/claude-setup";

const { useClaudeSetup } = await import("@platform/lib/claude-setup");
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
  let resyncs = 0;
  setEventsClientForTests({
    subscribe: ((topic: string, params: unknown, cb: Frame) => {
      const sub: Sub = { topic, params, cb, open: true };
      subs.push(sub);
      return () => {
        sub.open = false;
      };
    }) as never,
    resync: () => {
      resyncs += 1;
      return true;
    },
  });
  return {
    subs,
    get resyncs() {
      return resyncs;
    },
    open: () => subs.filter((x) => x.open),
    push: (snap: unknown) => {
      for (const x of subs) if (x.open) x.cb(snap, null, { gen: null });
    },
    fail: (error: string) => {
      for (const x of subs) if (x.open) x.cb(null, null, { error, status: 500 });
    },
  };
}

const IDLE_INSTALL = {
  action: null,
  state: "idle",
  detail: "",
  output: "",
  error: null,
  command: null,
  started_at: null,
  finished_at: null,
};
const RUNNING_INSTALL = { ...IDLE_INSTALL, action: "install", state: "running", detail: "downloading" };
const IDLE_LOGIN = { in_flight: false, started_at: null, error: null };
const OPEN_LOGIN = { in_flight: true, started_at: 1, error: null };

const HEALTH = { found: true, signed_in: true };

const realFetch = globalThis.fetch;
let healthRefreshes = 0;
function stubFetch(install: unknown, login: unknown) {
  healthRefreshes = 0;
  globalThis.fetch = (async (url: string) => {
    const path = url.split("?")[0]!;
    const body =
      path === "/api/claude/install"
        ? install
        : path === "/api/claude/login"
          ? login
          : path === "/api/claude/health/refresh"
            ? (healthRefreshes++, HEALTH)
            : path === "/api/claude/health"
              ? HEALTH
              : undefined;
    if (body === undefined) return new Response(JSON.stringify({ error: "unmocked" }), { status: 404 });
    return new Response(JSON.stringify(body), { status: 200 });
  }) as unknown as typeof fetch;
}

afterEach(() => {
  globalThis.fetch = realFetch;
  setEventsClientForTests(null);
});

async function mountHook() {
  let latest!: ClaudeSetup;
  const Probe = (): null => {
    latest = useClaudeSetup(false);
    return null;
  };
  let renderer!: ReturnType<typeof create>;
  await act(async () => {
    renderer = create(createElement(Probe));
  });
  await act(async () => {
    await new Promise((r) => setTimeout(r, 0));
  });
  return {
    get state() {
      return latest;
    },
    unmount: () => act(() => renderer.unmount()),
  };
}

test("nothing running: no claude.setup subscription at all", async () => {
  const bus = fakeEvents();
  stubFetch(IDLE_INSTALL, IDLE_LOGIN);
  const h = await mountHook();
  expect(bus.subs).toEqual([]);
  await h.unmount();
});

test("a running install follows claude.setup's .install, re-probes when done, and lets go", async () => {
  const bus = fakeEvents();
  stubFetch(RUNNING_INSTALL, IDLE_LOGIN);
  const h = await mountHook();
  expect(h.state.install?.state).toBe("running");
  expect(bus.open().map((x) => [x.topic, x.params])).toEqual([["claude.setup", null]]);

  await act(async () => {
    bus.push({ install: { ...RUNNING_INSTALL, detail: "unpacking" }, login: IDLE_LOGIN });
  });
  expect(h.state.install?.detail).toBe("unpacking");
  expect(bus.open().length).toBe(1);

  await act(async () => {
    bus.push({ install: { ...RUNNING_INSTALL, state: "done", detail: "installed" }, login: IDLE_LOGIN });
    await new Promise((r) => setTimeout(r, 0));
  });
  expect(h.state.install?.state).toBe("done");
  expect(healthRefreshes).toBe(1);
  expect(bus.open()).toEqual([]);
  await h.unmount();
});

test("an error frame is not a failed install: the record and the subscription stay", async () => {
  const bus = fakeEvents();
  stubFetch(RUNNING_INSTALL, IDLE_LOGIN);
  const h = await mountHook();
  await act(async () => {
    bus.fail("server unreachable");
  });
  expect(h.state.install?.state).toBe("running");
  expect(h.state.actionError).toBeNull();
  expect(bus.open().length).toBe(1);
  await h.unmount();
  expect(bus.open()).toEqual([]);
});

test("an open sign-in follows claude.setup's .login and re-probes when it stops being in flight", async () => {
  const bus = fakeEvents();
  stubFetch(IDLE_INSTALL, OPEN_LOGIN);
  const h = await mountHook();
  expect(h.state.login?.in_flight).toBe(true);
  expect(bus.open().map((x) => x.topic)).toEqual(["claude.setup"]);

  await act(async () => {
    bus.push({ install: IDLE_INSTALL, login: OPEN_LOGIN });
  });
  expect(bus.open().length).toBe(1);
  expect(healthRefreshes).toBe(0);

  await act(async () => {
    bus.push({ install: IDLE_INSTALL, login: { in_flight: false, started_at: 1, error: null } });
    await new Promise((r) => setTimeout(r, 0));
  });
  expect(h.state.login?.in_flight).toBe(false);
  expect(healthRefreshes).toBe(1);
  expect(bus.open()).toEqual([]);
  await h.unmount();
});

test("unmount mid-install closes the subscription", async () => {
  const bus = fakeEvents();
  stubFetch(RUNNING_INSTALL, IDLE_LOGIN);
  const h = await mountHook();
  expect(bus.open().length).toBe(1);
  await h.unmount();
  expect(bus.open()).toEqual([]);
});
