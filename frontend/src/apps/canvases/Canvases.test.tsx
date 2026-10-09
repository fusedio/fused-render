// The Canvases page's sign-in wait: after "Sign in to Fused" POSTs the CLI's
// browser login, the page follows the events bus's `canvases.status` topic
// until the login completes (a new credentials stamp) or the child exits —
// no 1.5 s status poll. Frames come from a scripted client installed with
// `setEventsClientForTests`; the page's own GETs/POSTs from a route-keyed
// `fetch` stub (never `mock.module`, which is process-wide in bun).
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const { default: Canvases } = await import("./Canvases");
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
  const resyncs: string[] = [];
  setEventsClientForTests({
    subscribe: ((topic: string, params: unknown, cb: Frame) => {
      const sub: Sub = { topic, params, cb, open: true };
      subs.push(sub);
      return () => {
        sub.open = false;
      };
    }) as never,
    resync: ((topic: string) => {
      resyncs.push(topic);
      return true;
    }) as never,
  });
  return {
    subs,
    resyncs,
    open: () => subs.filter((x) => x.open),
    push: (snap: unknown, meta: Record<string, unknown> = { gen: null }) =>
      act(async () => {
        for (const x of subs) if (x.open) x.cb(snap, null, meta);
      }),
  };
}

const STATUS = {
  cli_found: true,
  logged_in: false,
  creds_stamp: 3,
  login_in_flight: false,
  workbench_base_url: "https://www.fused.io",
  canvases_dir: "/tmp/canvases",
};

const realFetch = globalThis.fetch;
let hits: string[] = [];
function stubFetch(status: () => unknown) {
  hits = [];
  globalThis.fetch = (async (url: string) => {
    const path = url.split("?")[0]!;
    hits.push(path);
    const body =
      path === "/api/canvases/status"
        ? status()
        : path === "/api/canvases/login"
          ? { ok: true }
          : path === "/api/canvases/list"
            ? { canvases: [] }
            : { error: "unmocked" };
    return new Response(JSON.stringify(body), {
      status: path.startsWith("/api/canvases/") && body && !("error" in (body as object)) ? 200 : 404,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;
}

const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  globalThis.fetch = realFetch;
  setEventsClientForTests(null);
});

const settle = () =>
  act(async () => {
    await new Promise((done) => setTimeout(done, 0));
  });

const text = (node: ReactTestInstance): string =>
  node.children.map((c) => (typeof c === "string" ? c : text(c))).join("");

async function mountAndSignIn() {
  let current: unknown = STATUS;
  stubFetch(() => current);
  const bus = fakeEvents();
  let r!: ReactTestRenderer;
  await act(async () => {
    r = create(createElement(Canvases));
  });
  mounted.push(r);
  await settle();
  const button = r.root.find((n) => n.type === "button" && text(n) === "Sign in to Fused");
  // Nothing follows the status before a login is in flight.
  expect(bus.subs.filter((s) => s.topic === "canvases.status")).toHaveLength(0);
  await act(async () => {
    await (button.props.onClick as () => Promise<void>)();
  });
  await settle();
  return {
    r,
    bus,
    setStatus: (next: unknown) => {
      current = next;
    },
  };
}

test("Sign in subscribes to canvases.status and resyncs it after the POST", async () => {
  const { r, bus } = await mountAndSignIn();
  expect(hits).toContain("/api/canvases/login");
  expect(bus.open().map((s) => [s.topic, s.params])).toEqual([["canvases.status", {}]]);
  expect(bus.resyncs).toEqual(["canvases.status"]);
  expect(r.root.findAll((n) => n.type === "button" && text(n) === "Waiting for browser sign-in…")).toHaveLength(1);
});

test("a replayed snapshot does not end the wait; a new stamp completes it and closes the subscription", async () => {
  const { r, bus, setStatus } = await mountAndSignIn();
  // The sidebar's cached answer from before the POST: not in flight.
  await bus.push(STATUS, { gen: null, replay: true });
  expect(r.root.findAll((n) => n.type === "button" && text(n) === "Waiting for browser sign-in…")).toHaveLength(1);
  expect(JSON.stringify(r.toJSON())).not.toContain("Sign-in was not completed");

  await bus.push({ ...STATUS, login_in_flight: true });
  expect(bus.open()).toHaveLength(1);

  const statusReads = hits.filter((h) => h === "/api/canvases/status").length;
  setStatus({ ...STATUS, logged_in: true, creds_stamp: 4 });
  await bus.push({ ...STATUS, logged_in: true, creds_stamp: 4 });
  await settle();
  expect(bus.open()).toHaveLength(0);
  // The same refresh the poll's completion ran: status, then the listing.
  expect(hits.filter((h) => h === "/api/canvases/status").length).toBe(statusReads + 1);
  expect(hits).toContain("/api/canvases/list");
});

test("the child gone without a sign-in says so and closes the subscription", async () => {
  const { r, bus } = await mountAndSignIn();
  await bus.push({ ...STATUS, login_in_flight: false });
  expect(bus.open()).toHaveLength(0);
  expect(JSON.stringify(r.toJSON())).toContain("Sign-in was not completed — try again.");
  expect(r.root.findAll((n) => n.type === "button" && text(n) === "Sign in to Fused")).toHaveLength(1);
});

test("a refused frame is what a failed status GET was: nothing", async () => {
  const { r, bus } = await mountAndSignIn();
  await act(async () => {
    for (const x of bus.open()) x.cb(null, null, { error: "internal error", status: 500 });
  });
  expect(bus.open()).toHaveLength(1);
  expect(JSON.stringify(r.toJSON())).not.toContain("internal error");
});

test("unmounting mid-login closes the subscription", async () => {
  const { r, bus } = await mountAndSignIn();
  act(() => r.unmount());
  mounted.length = 0;
  expect(bus.open()).toHaveLength(0);
});

test("no timer re-asks the status any more", () => {
  const src = readFileSync(join(import.meta.dir, "Canvases.tsx"), "utf8");
  expect(src).not.toMatch(/setInterval|LOGIN_POLL_MS|pollRef/);
  expect(src).toContain('subscribeTopic<CanvasesStatus>("canvases.status"');
});
