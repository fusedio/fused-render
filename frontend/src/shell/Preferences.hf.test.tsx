// Preferences' Hugging Face section: while a device-code login is pending it
// follows `hf.auth` on the events bus (not a 2 s timer), and lets go once the
// login is over. Frames come from a scripted events client
// (`setEventsClientForTests`, never `mock.module`, which is process-wide in
// bun); HTTP is a route-keyed fetch stub.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

const { HuggingFaceSection } = await import("@shell/Preferences");
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

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
  setEventsClientForTests(null);
});

const SIGNED_OUT = { signedIn: false, account: null, source: null, forcedByVar: null, pending: null, error: null };
const PENDING = {
  ...SIGNED_OUT,
  pending: { userCode: "WXYZ-9876", url: "https://huggingface.co/device", secondsLeft: 600 },
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

async function mount() {
  let r!: ReactTestRenderer;
  await act(async () => {
    r = create(createElement(HuggingFaceSection));
  });
  await act(async () => {
    await new Promise((res) => setTimeout(res, 0));
  });
  return r;
}

async function click(r: ReactTestRenderer, label: string) {
  const btn = r.root.findAll((n) => n.type === "button" && JSON.stringify(n.props.children ?? "").includes(label))[0]!;
  await act(async () => {
    btn.props.onClick();
    await new Promise((res) => setTimeout(res, 0));
  });
}

test("signed out and idle: no hf.auth subscription", async () => {
  const bus = fakeEvents();
  stubFetch({ "/api/hf/auth": SIGNED_OUT });
  const r = await mount();
  expect(text(r)).toContain("Log in to Hugging Face");
  expect(bus.subs).toEqual([]);
  await act(async () => r.unmount());
});

test("a pending login follows hf.auth, ignores a blip, and closes once signed in", async () => {
  const bus = fakeEvents();
  stubFetch({ "/api/hf/auth": SIGNED_OUT, "/api/hf/login": { ...PENDING, joined: false } });
  const r = await mount();
  await click(r, "Log in to Hugging Face");
  expect(bus.open().map((x) => [x.topic, x.params])).toEqual([["hf.auth", null]]);
  expect(bus.resyncs).toBe(1);
  expect(text(r)).toContain("WXYZ-9876");

  await act(async () => bus.fail("network blip"));
  expect(text(r)).not.toContain("network blip");
  expect(bus.open().length).toBe(1);

  await act(async () => bus.push(SIGNED_IN));
  expect(text(r)).toContain("ana");
  expect(bus.open()).toEqual([]);
  await act(async () => r.unmount());
});

test("a login already pending on mount follows hf.auth; Cancel ends it and the subscription", async () => {
  const bus = fakeEvents();
  stubFetch({ "/api/hf/auth": PENDING, "/api/hf/login/cancel": SIGNED_OUT });
  const r = await mount();
  expect(bus.open().map((x) => x.topic)).toEqual(["hf.auth"]);
  await click(r, "Cancel");
  expect(bus.open()).toEqual([]);
  expect(text(r)).toContain("Log in to Hugging Face");
  await act(async () => r.unmount());
});

test("unmounting mid-login closes the subscription", async () => {
  const bus = fakeEvents();
  stubFetch({ "/api/hf/auth": PENDING });
  const r = await mount();
  expect(bus.open().length).toBe(1);
  await act(async () => r.unmount());
  expect(bus.open()).toEqual([]);
});
