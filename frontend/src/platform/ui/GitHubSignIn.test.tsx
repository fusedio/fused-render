import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestInstance } from "react-test-renderer";

const { GitHubSignIn } = await import("@platform/ui/GitHubSignIn");

const win = window as unknown as {
  setInterval: (fn: () => void, ms: number) => number;
  clearInterval: (id: number) => void;
};
const realFetch = globalThis.fetch;
const realSetInterval = win.setInterval;
const realClearInterval = win.clearInterval;
afterEach(() => {
  globalThis.fetch = realFetch;
  win.setInterval = realSetInterval;
  win.clearInterval = realClearInterval;
});

const armed = new Map<number, () => void>();
function trackIntervals() {
  armed.clear();
  let next = 1;
  win.setInterval = (fn) => {
    armed.set(next, fn);
    return next++;
  };
  win.clearInterval = (id) => void armed.delete(id);
}

type Reply = unknown | (() => unknown | Promise<unknown>);
function stubFetch(routes: Record<string, Reply>) {
  globalThis.fetch = (async (url: string, init?: RequestInit) => {
    const reply = routes[`${init?.method ?? "GET"} ${url}`];
    if (reply === undefined) return new Response("{}", { status: 404 });
    const body = typeof reply === "function" ? await (reply as () => unknown)() : reply;
    return new Response(JSON.stringify(body), { status: 200 });
  }) as unknown as typeof fetch;
}

function held() {
  let release!: (v: unknown) => void;
  const promise = new Promise((r) => (release = r));
  return { promise, release };
}

const text = (n: ReactTestInstance | string): string =>
  typeof n === "string" ? n : n.children.map(text).join("");
const flush = () => act(async () => {
  for (let i = 0; i < 5; i++) await new Promise((r) => setTimeout(r, 0));
});

async function mount(onCancel?: () => void) {
  let r!: ReturnType<typeof create>;
  await act(async () => {
    r = create(createElement(GitHubSignIn, { onSignedIn: () => {}, onCancel }));
  });
  await flush();
  const button = (label: string) =>
    r.root.find((n) => typeof n.props.onClick === "function" && text(n) === label);
  return {
    sentence: () => text(r.root.findByProps({ role: "status" })),
    click: (label: string) => act(async () => void button(label).props.onClick()),
    unmount: () => act(() => r.unmount()),
  };
}

const IDLE_INSTALL = { state: "idle", started_at: null };
const IDLE_LOGIN = { in_flight: false, started_at: null, error: null };

test("closing the sheet while the install POST is in flight never starts the poll", async () => {
  trackIntervals();
  const install = held();
  stubFetch({
    "GET /api/github/status": { found: false, signed_in: false, account: null },
    "GET /api/github/install": IDLE_INSTALL,
    "GET /api/github/login": IDLE_LOGIN,
    "POST /api/github/install": () => install.promise,
  });
  const h = await mount();
  expect(h.sentence()).toContain("set up GitHub");
  await h.click("Set up GitHub");
  await h.unmount();
  install.release({});
  await flush();
  expect(armed.size).toBe(0);
});

test("a poll tick in flight when the user cancels does not re-arm the poll or bring back waiting", async () => {
  trackIntervals();
  let login: unknown = { in_flight: true, started_at: 5, error: null, code: "ABCD-1234" };
  stubFetch({
    "GET /api/github/status": { found: true, signed_in: false, account: null },
    "GET /api/github/install": IDLE_INSTALL,
    "GET /api/github/login": () => login,
    "POST /api/github/login/cancel": { in_flight: false, started_at: 5, error: null },
  });
  const h = await mount(() => {});
  expect(h.sentence()).toContain("Paste this code");
  expect(armed.size).toBe(1);

  const tick = held();
  login = tick.promise;
  void [...armed.values()][0]!();
  await flush();
  await h.click("Cancel");
  await flush();
  tick.release({ in_flight: true, started_at: 5, error: null, code: "ABCD-1234" });
  await flush();

  expect(armed.size).toBe(0);
  expect(h.sentence()).toBe("Sign in to GitHub to continue.");
  await h.unmount();
});
