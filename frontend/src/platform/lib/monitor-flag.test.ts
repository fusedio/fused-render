// The flag's contract (monitor-flag.ts): `null` until the one prefs read lands,
// absence of the key reads as off, only `true` is on, a publish beats a read
// still in flight, and a failed read leaves the answer alone but retries on a
// timer. `fetch` is
// stubbed directly (getPrefs is a plain getJson), same as the dock tests.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { createElement } from "react";
import { publishMonitorEnabled, useMonitorFeature } from "@platform/lib/monitor-flag";

type Deferred = { resolve: (v: unknown) => void; reject: (e: Error) => void };

function stubFetch(): Deferred[] {
  const pending: Deferred[] = [];
  globalThis.fetch = (() =>
    new Promise((resolve, reject) => {
      pending.push({
        resolve: (body) => resolve({ ok: true, status: 200, json: async () => body }),
        reject,
      });
    })) as unknown as typeof fetch;
  return pending;
}

const seen: (boolean | null)[] = [];
function Probe() {
  seen.push(useMonitorFeature());
  return null;
}

async function mount(): Promise<ReactTestRenderer> {
  let r!: ReactTestRenderer;
  await act(async () => {
    r = create(createElement(Probe));
  });
  return r;
}
const flush = () => act(async () => {});
const last = () => seen[seen.length - 1];

test("null until the read lands; a payload without the key reads as off; true is on; a publish wins over a stale read", async () => {
  const pending = stubFetch();
  await mount();
  expect(last()).toBe(null);
  expect(pending).toHaveLength(1);

  await act(async () => pending[0].resolve({}));
  await flush();
  expect(last()).toBe(false);

  // The Preferences page turned it on, and the PUT's payload is the answer.
  await act(async () => publishMonitorEnabled(true));
  expect(last()).toBe(true);

  // A read that departed before the publish lands late with the old value:
  // ignored, because publish bumped the generation.
  const r2 = await mount();
  expect(last()).toBe(true);
  expect(pending).toHaveLength(1); // no second GET: the publish settled it
  r2.unmount();

  await act(async () => publishMonitorEnabled(false));
  expect(last()).toBe(false);
});

test("a failed read retries on a timer while the flag is still unanswered, so a never-remounting reader is not parked on null", async () => {
  // A fresh module instance (bun keys modules by specifier, query included):
  // the test above settled the shared one. A variable so tsc does not try to
  // resolve the query-string path.
  const specifier = "./monitor-flag.ts?retry";
  const fresh = (await import(specifier)) as typeof import("@platform/lib/monitor-flag");
  const timers: (() => void)[] = [];
  const realSetTimeout = globalThis.setTimeout;
  globalThis.setTimeout = ((fn: () => void) => {
    timers.push(fn);
    return 0;
  }) as unknown as typeof setTimeout;
  try {
    const pending = stubFetch();
    const values: (boolean | null)[] = [];
    function FreshProbe() {
      values.push(fresh.useMonitorFeature());
      return null;
    }
    let r!: ReactTestRenderer;
    await act(async () => {
      r = create(createElement(FreshProbe));
    });
    expect(pending).toHaveLength(1);

    await act(async () => pending[0].reject(new Error("offline")));
    await flush();
    expect(values[values.length - 1]).toBe(null);
    expect(pending).toHaveLength(1); // no hot loop: the retry waits for its timer

    await act(async () => timers.splice(0).forEach((fn) => fn()));
    expect(pending).toHaveLength(2);
    await act(async () => pending[1].resolve({ monitor: { enabled: true } }));
    await flush();
    expect(values[values.length - 1]).toBe(true);

    r.unmount();
  } finally {
    globalThis.setTimeout = realSetTimeout;
  }
});
