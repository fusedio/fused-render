// THE TIMELINE IS CACHED FOR THE LIFE OF THE PAGE (P4-22 / C G-14).
//
// T caches it and says why: "the target file never changes under it — so
// returning from a chat repaints rather than refetching. What invalidates that
// cache is a WRITE: `snapGoBack` repaints from the post-revert timeline the write
// itself returned, and a finished turn drops it (`snapInvalidate`). A failed read
// caches nothing" (T:19040-19047, 19105-19107).
//
// Natively the hook lives inside `Home`, which UNMOUNTS on the way into a chat,
// so component state could never be that cache and every Back spent the round
// trip again.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import type { SnapshotsTimeline } from "../protocol/types";

const { useSnapshots } = await import("./useSnapshots");
const { cachedSnapshots, resetSnapshotCacheForTests } = await import(
  "../protocol/snapshots"
);

const FILE = "/repo/x.py";

function timeline(hash: string): SnapshotsTimeline {
  return {
    file: FILE,
    hash,
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

const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  resetSnapshotCacheForTests();
  reads.length = 0;
  answer = () => Promise.resolve(timeline("h1"));
});

/** Every `loadSnapshots` this file's mounts have paid for. */
const reads: string[] = [];
let answer: () => Promise<SnapshotsTimeline> = () =>
  Promise.resolve(timeline("h1"));
const deps = {
  isFile: () => Promise.resolve(true),
  load: (_dir: string, file: string) => {
    reads.push(file);
    return answer();
  },
};

interface Harness {
  state(): import("./useSnapshots").SnapshotsState;
  unmount(): void;
}

async function mount(invalidation: unknown = 0): Promise<Harness> {
  let out: import("./useSnapshots").SnapshotsState | null = null;
  function Probe() {
    out = useSnapshots("/tpl", FILE, invalidation, deps as never);
    return null;
  }
  let r!: ReactTestRenderer;
  await act(async () => {
    r = create(createElement(Probe));
  });
  // The read is two awaits deep (the fileness stat, then the timeline).
  for (let i = 0; i < 4; i++) await act(async () => {});
  mounted.push(r);
  return {
    state: () => out!,
    unmount() {
      act(() => r.unmount());
      mounted.splice(mounted.indexOf(r), 1);
    },
  };
}

test("BACK REPAINTS, it does not refetch (T:19105-19107)", async () => {
  const first = await mount(0);
  expect(reads).toEqual([FILE]);
  expect(first.state().timeline?.hash).toBe("h1");

  // Entering a chat unmounts the panel; coming back mounts it again.
  first.unmount();
  const back = await mount(0);
  expect(reads).toEqual([FILE]);
  // And the rows are up on the FIRST paint — never the "reading the version
  // history…" note, which is the state this cache exists to skip.
  expect(back.state().timeline?.hash).toBe("h1");
  expect(back.state().failed).toBe(false);
});

test("A FINISHED TURN DROPS IT: a new invalidation is a new read", async () => {
  await mount(0);
  expect(reads.length).toBe(1);

  // `snapInvalidate` bumps a counter in the CHAT, which survives the panel — so
  // by the time the panel remounts there is nothing left to compare a "stale?"
  // flag against. The invalidation is part of the cache's KEY for that reason.
  mounted.splice(0).forEach((r) => act(() => r.unmount()));
  answer = () => Promise.resolve(timeline("h2"));
  const after = await mount(1);
  expect(reads.length).toBe(2);
  expect(after.state().timeline?.hash).toBe("h2");
});

test("A FAILED READ CACHES NOTHING (T:19044-19047)", async () => {
  answer = () => Promise.reject(new Error("store unreadable"));
  const h = await mount(0);
  expect(h.state().failed).toBe(true);
  expect(h.state().error).toContain("store unreadable");
  expect(cachedSnapshots(FILE, 0)).toBe(null);

  // So the next landing asks again rather than leaving the section stuck on the
  // failure for the life of the page.
  h.unmount();
  answer = () => Promise.resolve(timeline("h3"));
  const again = await mount(0);
  expect(reads.length).toBe(2);
  expect(again.state().timeline?.hash).toBe("h3");
  expect(again.state().failed).toBe(false);
});

test("the heading's RETRY spends the cache, or it would be a dead control", async () => {
  const h = await mount(0);
  expect(reads.length).toBe(1);
  answer = () => Promise.resolve(timeline("h4"));
  await act(async () => h.state().reload());
  for (let i = 0; i < 4; i++) await act(async () => {});
  expect(reads.length).toBe(2);
  expect(h.state().timeline?.hash).toBe("h4");
});

test("A WRITE'S OWN ANSWER BECOMES THE CACHE (T:19042-19043)", async () => {
  const h = await mount(0);
  const reverted = timeline("post-revert");
  await act(async () => h.state().adopt(reverted));
  expect(h.state().timeline?.hash).toBe("post-revert");
  // Not merely invalidated: the next landing repaints the post-revert chain
  // without a round trip, which is what "repaints from the timeline the write
  // itself returned" means past the end of that render.
  expect(cachedSnapshots(FILE, 0)?.hash).toBe("post-revert");
  h.unmount();
  const back = await mount(0);
  expect(reads.length).toBe(1);
  expect(back.state().timeline?.hash).toBe("post-revert");
});

test("the cache is keyed on the FILE as well: another target reads its own", async () => {
  await mount(0);
  expect(cachedSnapshots(FILE, 0)?.hash).toBe("h1");
  expect(cachedSnapshots("/repo/other.py", 0)).toBe(null);
});
