// The pulse store's fast lane: while a pulse reader is mounted and nobody else
// feeds the store, the store follows the document's listing feed
// (`syncFeedLane` → `subscribeListing`), and stands the feed down with the last
// reader. The re-entrancy this pins (merge audit, 2026-09-16): `subscribeListing`
// calls `schedule()` synchronously for its first subscriber, and `schedule()`
// comes back into `syncFeedLane` — which, with the slot still empty, subscribed a
// second no-op reader whose disposer was dropped, so `listingSubs` never returned
// to zero and the long-poll outlived every reader.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();

import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { listingFeedLive, resetListingFeedForTests, useTasksPulseRows } from "./tasksPulse";

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
  resetListingFeedForTests();
});

function Probe() {
  useTasksPulseRows();
  return null;
}

test("ONE feed subscription for the lane, and it ends with the last pulse reader", async () => {
  // The pulse read answers (empty), so `schedule()` runs and opens the lane;
  // everything after that — the listing read, the long-poll — is a server that
  // accepts and never answers. The lane must close on the readers alone, not
  // on anything the wire says.
  globalThis.fetch = ((url: string) =>
    String(url).includes("/api/tasks/pulse")
      ? Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ tasks: [] }) } as unknown as Response)
      : new Promise<Response>(() => {})) as unknown as typeof fetch;
  expect(listingFeedLive()).toBe(false);

  let r!: ReactTestRenderer;
  await act(async () => {
    r = create(<Probe />);
  });
  await act(async () => {
    await new Promise((done) => setTimeout(done, 20));
  });
  expect(listingFeedLive()).toBe(true);

  await act(async () => {
    r.unmount();
  });
  // Before the fix this stayed true for the life of the document.
  expect(listingFeedLive()).toBe(false);
});
