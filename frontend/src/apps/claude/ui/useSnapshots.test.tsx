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
  fileGate = null;
  answer = () => Promise.resolve(timeline("h1"));
});

/** Every `loadSnapshots` this file's mounts have paid for. */
const reads: string[] = [];
let answer: () => Promise<SnapshotsTimeline> = () =>
  Promise.resolve(timeline("h1"));
let fileGate: (() => void) | null = null;
const deps = {
  isFile: () => {
    if (fileGate) {
      return new Promise<boolean>((res) => {
        fileGate = () => res(true);
      });
    }
    return Promise.resolve(true);
  },
  load: (_dir: string, file: string) => {
    reads.push(file);
    return answer();
  },
};

interface Harness {
  state(): import("./useSnapshots").SnapshotsState;
  unmount(): void;
}

async function mount(
  invalidation: unknown = 0,
  enabled = true,
  file: string = FILE,
): Promise<Harness> {
  let out: import("./useSnapshots").SnapshotsState | null = null;
  function Probe() {
    out = useSnapshots("/tpl", file, invalidation, deps as never, enabled);
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

// ── the gate `useLandingReads` holds it behind (P4-14) ──────────────────────

test("`enabled: false` reads nothing and is NOT settled", async () => {
  const h = await mount(0, false);
  expect(reads).toEqual([]);
  // "Not asked yet" is exactly what the ordering owner is waiting on: reporting
  // settled here would release the artifacts index ahead of this read.
  expect(h.state().settled).toBe(false);
  expect(h.state().timeline).toBeUndefined();
});

test("a target with no file is SETTLED, so nothing is stranded behind it", async () => {
  const h = await mount(0, true, "");
  expect(h.state().timeline).toBeUndefined();
  expect(h.state().settled).toBe(true);
});

test("A FAILURE IS AN ANSWER for the ordering's purposes", async () => {
  answer = () => Promise.reject(new Error("nope"));
  const h = await mount(0);
  expect(h.state().failed).toBe(true);
  expect(h.state().settled).toBe(true);
});

test("SETTLED GOES FALSE BEFORE THE FILENESS STAT, not after it", async () => {
  // The stat is an await, and `settled` is what releases the artifacts index
  // (`useLandingReads`). Reset only on the far side of it, a target switch
  // carried the previous file's `true` across the whole wait and the last of
  // the three reads went first (Bugbot, this batch).
  const first = await mount(0);
  expect(first.state().settled).toBe(true);
  first.unmount();

  // A second target, with the stat wedged open.
  fileGate = () => {};
  let out: import("./useSnapshots").SnapshotsState | null = null;
  function Probe() {
    out = useSnapshots("/tpl", "/repo/other.py", 0, deps as never, true);
    return null;
  }
  let r!: ReactTestRenderer;
  await act(async () => {
    r = create(createElement(Probe));
  });
  mounted.push(r);
  for (let i = 0; i < 3; i++) await act(async () => {});
  // Mid-stat: nothing may be released.
  expect(out!.settled).toBe(false);
  const open = fileGate;
  fileGate = null;
  await act(async () => open?.());
  for (let i = 0; i < 4; i++) await act(async () => {});
  expect(out!.settled).toBe(true);
});
