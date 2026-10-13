// HubSearchScreen's two live facts, on the events bus instead of a timer:
//   * HubLogin follows `hf.auth` only while a device-code login is pending;
//   * the pool re-search is re-asked each time `ai.hubcache {capability}`
//     moves while the pool builds (`followHubCache`; the search stays a POST,
//     D11).
// Frames come from a scripted events client (`setEventsClientForTests`) or
// the helper's own `subscribe` seam — never `mock.module`, which is
// process-wide in bun. HTTP is a route-keyed fetch stub.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { SubscribeLike } from "@platform/lib/events";

const { HubLogin, followHubCache } = await import("./HubSearchScreen");
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
  const subscribe: SubscribeLike = (topic, params, cb) => {
    const sub: Sub = { topic, params, cb: cb as Frame, open: true };
    subs.push(sub);
    return () => {
      sub.open = false;
    };
  };
  setEventsClientForTests({
    subscribe: subscribe as never,
    resync: () => {
      resyncs += 1;
      return true;
    },
  });
  return {
    subs,
    subscribe,
    get resyncs() {
      return resyncs;
    },
    open: () => subs.filter((x) => x.open),
    push: (snap: unknown, meta: Record<string, unknown> = { gen: null }) => {
      for (const x of subs) if (x.open) x.cb(snap, null, meta);
    },
    fail: (error: string) => {
      for (const x of subs) if (x.open) x.cb(null, null, { error, status: 500 });
    },
  };
}

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
  setEventsClientForTests(null);
});

const SIGNED_OUT = { signedIn: false, account: null, source: null, forcedByVar: null, pending: null, error: null };
const PENDING = {
  ...SIGNED_OUT,
  pending: { userCode: "ABCD-1234", url: "https://huggingface.co/device", secondsLeft: 900 },
};
const SIGNED_IN = { ...SIGNED_OUT, signedIn: true, account: "ana", source: "login" };

function stubFetch(routes: Record<string, unknown>) {
  globalThis.fetch = (async (url: string) => {
    const body = routes[url.split("?")[0]!];
    if (body === undefined) return new Response(JSON.stringify({ error: "unmocked" }), { status: 404 });
    return new Response(JSON.stringify(body), { status: 200 });
  }) as unknown as typeof fetch;
}

const text = (r: ReactTestRenderer) => JSON.stringify(r.toJSON());
const clickButton = async (r: ReactTestRenderer, label: string) => {
  const btn = r.root.findAll((n) => n.type === "button" && JSON.stringify(n.props.children ?? "").includes(label))[0]!;
  await act(async () => {
    btn.props.onClick();
    await new Promise((res) => setTimeout(res, 0));
  });
};

test("HubLogin: no subscription until a login is pending; then hf.auth until signed in", async () => {
  const bus = fakeEvents();
  stubFetch({ "/api/hf/login": { ...PENDING, joined: false } });
  let signedIn = 0;
  let r!: ReactTestRenderer;
  await act(async () => {
    r = create(createElement(HubLogin, { onSignedIn: () => (signedIn += 1) }));
  });
  expect(bus.subs).toEqual([]);

  await clickButton(r, "Hugging Face");
  expect(bus.open().map((x) => [x.topic, x.params])).toEqual([["hf.auth", null]]);
  // The POST that started the login asks the bus for a fresh snapshot once.
  expect(bus.resyncs).toBe(1);
  expect(text(r)).toContain("ABCD-1234");

  // A blip mid-login is not worth a banner, and does not end the wait.
  await act(async () => bus.fail("network blip"));
  expect(text(r)).not.toContain("network blip");
  expect(bus.open().length).toBe(1);

  await act(async () => bus.push(PENDING));
  expect(bus.open().length).toBe(1);

  await act(async () => bus.push(SIGNED_IN));
  expect(signedIn).toBe(1);
  expect(bus.open()).toEqual([]);
  await act(async () => r.unmount());
});

test("HubLogin: unmounting mid-login closes the subscription", async () => {
  const bus = fakeEvents();
  stubFetch({ "/api/hf/login": { ...PENDING, joined: false } });
  let r!: ReactTestRenderer;
  await act(async () => {
    r = create(createElement(HubLogin, { onSignedIn: () => {} }));
  });
  await clickButton(r, "Hugging Face");
  expect(bus.open().length).toBe(1);
  await act(async () => r.unmount());
  expect(bus.open()).toEqual([]);
});

test("followHubCache: opens ai.hubcache for the capability; the first frame is the baseline", () => {
  const bus = fakeEvents();
  let moved = 0;
  const off = followHubCache("embeddings", () => (moved += 1), bus.subscribe);
  expect(bus.subs.map((x) => [x.topic, x.params])).toEqual([["ai.hubcache", { capability: "embeddings" }]]);
  const building = (pages: number) => ({ state: "building", pagesDone: pages, startedAt: 1, blockedUntil: null });
  bus.push(building(1));
  expect(moved).toBe(0);
  // An identical snapshot (a replay, a resync) is not a move.
  bus.push(building(1), { replay: true });
  expect(moved).toBe(0);
  bus.push(building(2));
  expect(moved).toBe(1);
  // An error frame: the server says again; nothing re-asked.
  bus.fail("boom");
  expect(moved).toBe(1);
  // The build finishing is a move too — the re-search is what picks up the
  // finished catalog (and its "ready" poolState then closes the follow).
  bus.push({ state: "none", pagesDone: null, startedAt: 1, blockedUntil: null });
  expect(moved).toBe(2);
  off();
  expect(bus.open()).toEqual([]);
});

test("followHubCache: a first frame that already says the build finished is a move", () => {
  // The catalog finished between the POST that reported "building" and this
  // subscribe; the server pushes only on change, so the re-search that picks
  // up the finished catalog has to come from this first frame.
  const bus = fakeEvents();
  let moved = 0;
  const off = followHubCache("embeddings", () => (moved += 1), bus.subscribe);
  bus.push({ state: "none", pagesDone: null, startedAt: 1, blockedUntil: null });
  expect(moved).toBe(1);
  bus.push({ state: "none", pagesDone: null, startedAt: 1, blockedUntil: null }, { replay: true });
  expect(moved).toBe(1);
  off();
});
