// THE ORDER OF THE LANDING'S THREE READS (P4-14 / C G-1).
//
// T sequences them and argues each step (T:19282-19293): `await loadRecent()` →
// ready → `watchRecent()` → `await mountSnapshots()` → `loadArtifacts()`
// unawaited and last, "the only one that leaves the machine … so it must not sit
// in front of the session list". Native fired all three as independent effects
// on one commit.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import type { SnapshotsTimeline } from "../protocol/types";

const { useLandingReads } = await import("./useLandingReads");
const { resetSnapshotCacheForTests } = await import("../protocol/snapshots");
const { resetArtifactsMemoryForTests } = await import("./useArtifacts");

const FILE = "/repo/x.py";

function timeline(): SnapshotsTimeline {
  return {
    file: FILE,
    hash: "h",
    available: true,
    writable: true,
    writable_reason: "",
    current: { exists: true, size: 1, lines: 1 },
    versions: [],
    position: null,
    revert: null,
    offer: true,
    offer_reason: "",
    at_earliest: false,
    unconfirmed: false,
    blocking: [],
    enriched: false,
    unique_current: false,
    skipped: [],
    note: "",
  } as SnapshotsTimeline;
}

/** Every read, in the order it was ISSUED — which is the whole assertion. */
let order: string[] = [];
let snapGate: (() => void) | null = null;

const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  resetSnapshotCacheForTests();
  resetArtifactsMemoryForTests();
  order = [];
  snapGate = null;
});

async function mount(recent: unknown[] | null) {
  let out: import("./useLandingReads").LandingReads | null = null;
  function Probe(p: { recent: unknown[] | null }) {
    // `useSnapshots` and `useArtifacts` are reached through `useLandingReads`,
    // which is the unit under test; their own reads are the seams.
    out = useLandingReads(
      "/tpl",
      FILE,
      p.recent,
      0,
      // Both hooks' seams, handed in through the owner so the ORDER is what is
      // observed rather than either hook in isolation.
      {
        snaps: {
          isFile: () => Promise.resolve(true),
          load: () => {
            order.push("snapshots");
            if (snapGate) {
              return new Promise<SnapshotsTimeline>((res) => {
                snapGate = () => res(timeline());
              });
            }
            return Promise.resolve(timeline());
          },
        },
        artifacts: () => {
          order.push("artifacts");
          return Promise.resolve([]);
        },
      },
    );
    return null;
  }
  let r!: ReactTestRenderer;
  await act(async () => {
    r = create(createElement(Probe, { recent }));
  });
  for (let i = 0; i < 5; i++) await act(async () => {});
  mounted.push(r);
  return {
    reads: () => out!,
    async setRecent(next: unknown[] | null) {
      await act(async () => {
        r.update(createElement(Probe, { recent: next }));
      });
      for (let i = 0; i < 5; i++) await act(async () => {});
    },
    async release() {
      const open = snapGate;
      snapGate = null;
      await act(async () => open?.());
      for (let i = 0; i < 5; i++) await act(async () => {});
    },
  };
}

test("NOTHING GOES BEFORE THE SESSION LIST", async () => {
  const h = await mount(null);
  // `recent === null` is "the read has not answered". T awaits it first, and
  // the host uncovers the pane on the signal AFTER it.
  expect(order).toEqual([]);
  expect(h.reads().sessionsIn).toBe(false);
});

test("THEN SNAPSHOTS, THEN ARTIFACTS — the remote call last (T:19290-19293)", async () => {
  const h = await mount(null);
  snapGate = () => {};
  await h.setRecent([]);
  // The local checkpoint read goes, and the artifacts index — the only one that
  // leaves the machine — is still held.
  expect(order).toEqual(["snapshots"]);
  await h.release();
  expect(order).toEqual(["snapshots", "artifacts"]);
});

test("a list that answers EMPTY still releases the two below it", async () => {
  // `[]` is an answer: a folder with no chats is the common case, and holding
  // the other two behind it would make a quiet target a slow one.
  const h = await mount([]);
  expect(order).toEqual(["snapshots", "artifacts"]);
  expect(h.reads().sessionsIn).toBe(true);
});

test("A TARGET WITH NO PANEL IS SETTLED, so artifacts is not stranded", async () => {
  // `snaps.settled` is the release, and it is true for "no panel here" and for
  // a failure as well as for an answer — otherwise a read that is never going
  // to happen holds the last one for ever.
  const h = await mount([]);
  expect(h.reads().snaps.settled).toBe(true);
  expect(order).toContain("artifacts");
});
