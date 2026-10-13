// The pulse store's lane: while a pulse reader is mounted the store follows
// the document's listing feed (`syncFeedLane` → `subscribeListing`), and stands
// the feed down with the last reader. The re-entrancy this pins (merge audit,
// 2026-09-16): `subscribeListing` calls `schedule()` synchronously for its first
// subscriber, and `schedule()` comes back into `syncFeedLane` — which, with the
// slot still empty, subscribed a second no-op reader whose disposer was dropped,
// so `listingSubs` never returned to zero and the subscription outlived every
// reader.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();

import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { listingFeedLive, resetListingFeedForTests, useTasksPulseRows } from "./tasksPulse";

afterEach(() => {
  resetListingFeedForTests();
});

function Probe() {
  useTasksPulseRows();
  return null;
}

test("ONE feed subscription for the lane, and it ends with the last pulse reader", async () => {
  // The events client in this process has no socket to dial (bun has no
  // `location.host`), so the lane must open and close on the readers alone,
  // not on anything the wire says.
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
