// Preferences' Fused account tab: while the CLI's browser login is in flight
// the section follows the events bus's `canvases.status` topic — no 1.5 s
// status poll — and only `refresh` (whoami-vouched) writes what it shows.
// Frames come from a scripted client installed with `setEventsClientForTests`;
// the GETs/POSTs from a route-keyed `fetch` stub (never `mock.module`, which is
// process-wide in bun).
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const { FusedAccountSection } = await import("./FusedAccountSection");
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
function stubFetch(routes: Record<string, () => unknown>) {
  hits = [];
  globalThis.fetch = (async (url: string) => {
    const path = url.split("?")[0]!;
    hits.push(path);
    const route = routes[path] as (() => unknown) | undefined;
    return new Response(JSON.stringify(route ? route() : { error: "unmocked" }), {
      status: route ? 200 : 404,
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
const button = (r: ReactTestRenderer, label: string) =>
  r.root.findAll((n) => n.type === "button" && text(n).trim() === label);

async function mountAndSignIn() {
  let current: unknown = STATUS;
  stubFetch({
    "/api/canvases/status": () => current,
    "/api/canvases/login": () => ({ ok: true }),
    "/api/canvases/login/cancel": () => ({ ok: true }),
    "/api/canvases/whoami": () => ({ handle: "ada" }),
  });
  const bus = fakeEvents();
  let r!: ReactTestRenderer;
  await act(async () => {
    r = create(createElement(FusedAccountSection));
  });
  mounted.push(r);
  await settle();
  expect(bus.subs).toHaveLength(0); // idle: no subscription for a flow nobody started
  await act(async () => {
    (button(r, "Sign in to Fused")[0].props.onClick as () => void)();
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
  expect(button(r, "Cancel")).toHaveLength(1);
});

test("a new stamp completes the login: refresh writes the vouched status, the subscription closes", async () => {
  const { r, bus, setStatus } = await mountAndSignIn();
  await bus.push(STATUS, { gen: null, replay: true }); // pre-POST cache: not a verdict
  await bus.push({ ...STATUS, login_in_flight: true });
  expect(button(r, "Cancel")).toHaveLength(1);
  setStatus({ ...STATUS, logged_in: true, creds_stamp: 4 });
  await bus.push({ ...STATUS, logged_in: true, creds_stamp: 4 });
  await settle();
  expect(bus.open()).toHaveLength(0);
  expect(hits).toContain("/api/canvases/whoami");
  expect(button(r, "Log out")).toHaveLength(1);
  expect(JSON.stringify(r.toJSON())).toContain("ada");
});

test("the child gone without a sign-in says so and closes the subscription", async () => {
  const { r, bus } = await mountAndSignIn();
  await bus.push({ ...STATUS, login_in_flight: false });
  expect(bus.open()).toHaveLength(0);
  expect(JSON.stringify(r.toJSON())).toContain("Sign-in was not completed — try again.");
  expect(button(r, "Sign in to Fused")).toHaveLength(1);
});

test("a refused frame is a blip mid-login, not a banner", async () => {
  const { r, bus } = await mountAndSignIn();
  await act(async () => {
    for (const x of bus.open()) x.cb(null, null, { error: "internal error", status: 500 });
  });
  expect(bus.open()).toHaveLength(1);
  expect(JSON.stringify(r.toJSON())).not.toContain("internal error");
});

test("Cancel closes the subscription first, then resyncs after the cancel POST", async () => {
  const { r, bus } = await mountAndSignIn();
  await act(async () => {
    (button(r, "Cancel")[0].props.onClick as () => void)();
  });
  await settle();
  expect(bus.open()).toHaveLength(0);
  expect(hits).toContain("/api/canvases/login/cancel");
  expect(bus.resyncs).toEqual(["canvases.status", "canvases.status"]);
  expect(JSON.stringify(r.toJSON())).not.toContain("Sign-in was not completed");
});

test("unmounting mid-login closes the subscription", async () => {
  const { r, bus } = await mountAndSignIn();
  act(() => r.unmount());
  mounted.length = 0;
  expect(bus.open()).toHaveLength(0);
});

test("no timer re-asks the status any more", () => {
  const src = readFileSync(join(import.meta.dir, "FusedAccountSection.tsx"), "utf8");
  expect(src).not.toMatch(/setInterval|LOGIN_POLL_MS/);
  expect(src).toContain('subscribeTopic<CanvasesStatus>("canvases.status"');
});
