// The Left sidebar preview flag (left-rail-peek-flag): off until a read says
// on, a publish beats a slower read, and a re-read on the window's return
// beats a first read still out — the older answer landing later is old news.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

const { useLeftRailPeek, leftRailPeekNow, publishLeftRailPeek, resetLeftRailPeekFlagForTests, rereadLeftRailPeekForTests } = await import(
  "./left-rail-peek-flag"
);

/** Each fetch takes the next answer off this queue (a promise the test
 *  resolves when it wants). */
let answers: Promise<unknown>[] = [];
const realFetch = globalThis.fetch;
beforeEach(() => {
  resetLeftRailPeekFlagForTests();
  answers = [];
  (globalThis as { fetch: unknown }).fetch = async () => {
    const body = await (answers.shift() ?? Promise.resolve({}));
    return { ok: true, json: async () => body } as unknown as Response;
  };
});
const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  resetLeftRailPeekFlagForTests();
  (globalThis as { fetch: unknown }).fetch = realFetch;
});

function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
}
const tick = () => act(async () => void (await new Promise((r) => setTimeout(r, 0))));

async function mount() {
  function Probe() {
    useLeftRailPeek();
    return null;
  }
  await act(async () => {
    mounted.push(create(<Probe />));
  });
}

test("off until the read says on", async () => {
  answers = [Promise.resolve({ left_rail_peek: { enabled: true } })];
  expect(leftRailPeekNow()).toBe(false);
  await mount();
  await tick();
  expect(leftRailPeekNow()).toBe(true);
});

test("a publish beats a slower read still out", async () => {
  const slow = deferred<unknown>();
  answers = [slow.promise];
  await mount();
  publishLeftRailPeek({ enabled: true });
  slow.resolve({ left_rail_peek: { enabled: false } });
  await tick();
  expect(leftRailPeekNow()).toBe(true);
});

test("a re-read on the window's return beats the first read landing after it", async () => {
  const first = deferred<unknown>();
  const again = deferred<never>();
  answers = [first.promise];
  await mount();
  // The window comes back while the first read is still out: a fresh GET
  // (platform/lib/prefs-reread hands every flag module the one read).
  rereadLeftRailPeekForTests(again.promise);
  again.resolve({ left_rail_peek: { enabled: true } } as never);
  await tick();
  expect(leftRailPeekNow()).toBe(true);
  first.resolve({ left_rail_peek: { enabled: false } });
  await tick();
  expect(leftRailPeekNow()).toBe(true);
});

test("a failed re-read changes nothing, and asks again if it had outdated the only answer coming", async () => {
  const first = deferred<unknown>();
  answers = [first.promise, Promise.resolve({ left_rail_peek: { enabled: true } })];
  await mount();
  rereadLeftRailPeekForTests(Promise.reject(new Error("offline")));
  await tick();
  // The outdated first read lands: ignored; the fresh one asked for answers.
  first.resolve({ left_rail_peek: { enabled: false } });
  await tick();
  expect(leftRailPeekNow()).toBe(true);
});
