// The flag's contract (monitor-flag.ts): `null` until the one prefs read lands,
// absence of the key reads as off, only `true` is on, a publish beats a read
// still in flight, and a failed read leaves the answer alone. `fetch` is
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
