// The listing feed: one read and one long-poll for the document, however many
// readers — and the merge, the generation guard and the replay that make that
// safe to share.
import { afterEach, describe, expect, test } from "bun:test";

import type { Task } from "@platform/lib/api";
import {
  CHANGES_BACKOFF_MS,
  LISTING_FLOOR_MS,
  dropListingKeys,
  listingFeedLive,
  onGone,
  readListing,
  refreshListing,
  resetListingFeedForTests,
  subscribeListing,
  type ListingEnv,
  type ListingEvent,
} from "./tasksPulse";

const row = (key: string, last_active = 1): Task =>
  ({ key, task_id: key.toUpperCase(), project: "/proj", session_id: key, last_active }) as Task;

/** A scripted change-poll and a scripted listing, with every timer recorded
 *  rather than run. */
function env(script: unknown[], listings: unknown[]) {
  const urls: string[] = [];
  const waits: number[] = [];
  const floors: number[] = [];
  let pokeFn: (() => void) | null = null;
  let floorFn: (() => void) | null = null;
  let reads = 0;
  let i = 0;
  const e: ListingEnv & {
    urls: string[];
    waits: number[];
    floors: number[];
    readCount(): number;
    poke(): void;
    tick(): void;
  } = {
    urls,
    waits,
    floors,
    readCount: () => reads,
    poke: () => pokeFn?.(),
    tick: () => floorFn?.(),
    fetch: (url) => {
      urls.push(url);
      const next = script[i++];
      if (next === "boom") return Promise.reject(new Error("offline"));
      if (next === undefined) return new Promise(() => {}); // park forever
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(next) });
    },
    hidden: () => false,
    whenVisible: () => ({ promise: new Promise<void>(() => {}), cancel: () => {} }),
    sleep: (ms) => {
      waits.push(ms);
      return Promise.resolve();
    },
    tasks: () => {
      const answer = listings[Math.min(reads++, listings.length - 1)];
      if (answer === "boom") return Promise.reject(new Error("no such folder"));
      return Promise.resolve(answer as { tasks?: Task[]; generation?: number });
    },
    pokes: (fn) => {
      pokeFn = fn;
      return () => {
        pokeFn = null;
      };
    },
    every: (ms, fn) => {
      floors.push(ms);
      floorFn = fn;
      return () => {
        floorFn = null;
      };
    },
  };
  return e;
}

const settle = async () => {
  for (let i = 0; i < 40; i++) await Promise.resolve();
};

afterEach(() => {
  resetListingFeedForTests();
});

describe("subscribeListing", () => {
  test("N subscribers, ONE listing read and ONE long-poll", async () => {
    const e = env([{ generation: 1 }], [{ tasks: [row("a")] }]);
    const seen: ListingEvent[][] = [[], [], []];
    const offs = seen.map((bucket) => subscribeListing((ev) => bucket.push(ev), e));
    await settle();
    // The cards wall's whole bug in one assertion: twelve mounts used to be
    // twelve of each of these.
    expect(e.readCount()).toBe(1);
    // ONE loop, which is one handshake (`since=-1`) and then the real wait.
    // Three loops would open three handshakes.
    expect(e.urls).toEqual([
      "/api/tasks/changes?since=-1&wait=25",
      "/api/tasks/changes?since=1&wait=25",
    ]);
    // …and every reader got the rows.
    for (const bucket of seen) {
      expect(bucket[bucket.length - 1].rows.map((t) => t.key)).toEqual(["a"]);
    }
    for (const off of offs) off();
    expect(listingFeedLive()).toBe(false);
  });

  test("a late subscriber is replayed the rows it missed, synchronously", async () => {
    const e = env([], [{ tasks: [row("a")] }]);
    const first = subscribeListing(() => {}, e);
    await settle();
    const late: ListingEvent[] = [];
    const second = subscribeListing((ev) => late.push(ev), e);
    // No await: a card mounted five minutes in must not wear a skeleton until
    // something happens to change.
    expect(late.length).toBe(1);
    expect(late[0].rows.map((t) => t.key)).toEqual(["a"]);
    expect(late[0].delta).toBeNull();
    expect(e.readCount()).toBe(1); // and it cost no second read
    first();
    second();
  });

  test("the poll stops with the last subscriber and starts again with the next", async () => {
    const e = env([{ generation: 1 }], [{ tasks: [] }]);
    const off = subscribeListing(() => {}, e);
    await settle();
    expect(listingFeedLive()).toBe(true);
    off();
    expect(listingFeedLive()).toBe(false);
    const e2 = env([], [{ tasks: [row("b")] }]);
    const off2 = subscribeListing(() => {}, e2);
    await settle();
    expect(e2.readCount()).toBe(1);
    off2();
  });

  test("a delta is MERGED, not re-read: the rows fold in and the order holds", async () => {
    const e = env(
      [{ generation: 1 }, { generation: 2, rows: [row("b", 9)] }],
      [{ tasks: [row("a", 1)] }],
    );
    const seen: ListingEvent[] = [];
    const off = subscribeListing((ev) => seen.push(ev), e);
    await settle();
    off();
    // ONE listing read for both events — the delta cost no `GET /api/tasks`.
    expect(e.readCount()).toBe(1);
    const last = seen[seen.length - 1];
    // `last_active` descending, the one ordering promise mergeTaskChanges keeps.
    expect(last.rows.map((t) => t.key)).toEqual(["b", "a"]);
    expect(last.delta?.rows.map((t) => t.key)).toEqual(["b"]);
  });

  test("a `gone` key leaves the rows and reaches onGone", async () => {
    const e = env([{ generation: 1 }, { generation: 2, gone: ["a"] }], [{ tasks: [row("a")] }]);
    const gone: string[][] = [];
    const offGone = onGone((keys) => gone.push(keys));
    const seen: ListingEvent[] = [];
    const off = subscribeListing((ev) => seen.push(ev), e);
    await settle();
    off();
    offGone();
    expect(seen[seen.length - 1].rows).toEqual([]);
    expect(gone).toEqual([["a"]]);
  });

  test("a full listing OLDER than a delta already folded in is dropped", async () => {
    // The delta takes the feed to generation 5; the refresh that follows answers
    // from generation 4 — a read that left before it. Applying it would roll the
    // rows back (bugbot #892).
    const e = env(
      [{ generation: 1 }, { generation: 5, rows: [row("b", 9)] }],
      [
        { tasks: [row("a", 1)], generation: 1 },
        { tasks: [row("a", 1)], generation: 4 },
      ],
    );
    const seen: ListingEvent[] = [];
    const off = subscribeListing((ev) => seen.push(ev), e);
    await settle();
    refreshListing();
    await settle();
    off();
    expect(e.readCount()).toBe(2); // the stale read HAPPENED…
    // …and was thrown away: the merged rows are still what is held.
    expect(seen[seen.length - 1].rows.map((t) => t.key)).toEqual(["b", "a"]);
    expect(readListing()?.map((t) => t.key)).toEqual(["b", "a"]);
  });

  test("`full` forgets the generation first, so the catch-up read is accepted", async () => {
    const e = env(
      [{ generation: 1 }, { generation: 9, rows: [row("b", 9)] }, { generation: 2, full: true }],
      [
        { tasks: [row("a")], generation: 1 },
        // A server that restarted and counts from zero again.
        { tasks: [row("c")], generation: 2 },
      ],
    );
    const seen: ListingEvent[] = [];
    const off = subscribeListing((ev) => seen.push(ev), e);
    await settle();
    off();
    expect(seen[seen.length - 1].rows.map((t) => t.key)).toEqual(["c"]);
  });

  test("a burst of pokes is ONE read", async () => {
    const e = env([{ generation: 1 }], [{ tasks: [row("a")] }]);
    const off = subscribeListing(() => {}, e);
    await settle();
    expect(e.readCount()).toBe(1);
    // `focus`, `storage` and `tasks-changed` all fire for one turn ending.
    e.poke();
    e.poke();
    e.poke();
    await settle();
    expect(e.readCount()).toBe(2);
    off();
  });

  test("a poke after the last unsubscribe reads nothing", async () => {
    const e = env([{ generation: 1 }], [{ tasks: [] }]);
    const off = subscribeListing(() => {}, e);
    await settle();
    off();
    refreshListing();
    await settle();
    expect(e.readCount()).toBe(1);
  });

  test("a failed read is `[]` and `failed`, and forgets the rows it was holding", async () => {
    const e = env([], [{ tasks: [row("a")] }, "boom"]);
    const seen: ListingEvent[] = [];
    const off = subscribeListing((ev) => seen.push(ev), e);
    await settle();
    refreshListing();
    await settle();
    off();
    const last = seen[seen.length - 1];
    expect(last.rows).toEqual([]);
    expect(last.failed).toBe(true);
    // Never rows kept over a server that has since gone away (#1079).
    expect(readListing()).toBeNull();
  });

  test("the floor refresh is its own clock, at the page's old 20s", async () => {
    const e = env([{ generation: 1 }], [{ tasks: [row("a")] }]);
    const off = subscribeListing(() => {}, e);
    await settle();
    expect(e.floors).toEqual([LISTING_FLOOR_MS]);
    e.tick();
    await settle();
    expect(e.readCount()).toBe(2);
    off();
  });

  test("a failed change-poll backs off and carries on", async () => {
    const e = env([{ generation: 1 }, "boom", { generation: 2, rows: [row("b", 9)] }], [
      { tasks: [row("a")] },
    ]);
    const seen: ListingEvent[] = [];
    const off = subscribeListing((ev) => seen.push(ev), e);
    await settle();
    off();
    expect(e.waits).toContain(CHANGES_BACKOFF_MS);
    expect(seen[seen.length - 1].rows.map((t) => t.key)).toEqual(["b", "a"]);
  });

  test("a hidden tab sits the long-poll out entirely", async () => {
    const e = env([{ generation: 1 }], [{ tasks: [] }]);
    const off = subscribeListing(() => {}, { ...e, hidden: () => true });
    await settle();
    off();
    expect(e.urls.length).toBe(0);
  });

  test("unsubscribing aborts the in-flight long-poll", async () => {
    let signal: AbortSignal | undefined;
    const e = env([{ generation: 1 }], [{ tasks: [] }]);
    const off = subscribeListing(() => {}, {
      ...e,
      fetch: (url, init) => {
        signal = init?.signal;
        return e.fetch(url, init);
      },
    });
    await settle();
    off();
    expect(signal?.aborted).toBe(true);
  });

  test("rows with no key are dropped from the listing rather than rendered", async () => {
    const e = env([], [{ tasks: [row("a"), null, { title: "no key" }] }]);
    const seen: ListingEvent[] = [];
    const off = subscribeListing((ev) => seen.push(ev), e);
    await settle();
    off();
    expect(seen[seen.length - 1].rows.map((t) => t.key)).toEqual(["a"]);
  });
});

describe("dropListingKeys", () => {
  test("takes the row off the held listing and announces it as gone", async () => {
    const e = env([], [{ tasks: [row("a"), row("b")] }]);
    const gone: string[][] = [];
    const offGone = onGone((keys) => gone.push(keys));
    const seen: ListingEvent[] = [];
    const off = subscribeListing((ev) => seen.push(ev), e);
    await settle();

    dropListingKeys(["a"]);
    const last = seen[seen.length - 1];
    expect(last.rows.map((t) => t.key)).toEqual(["b"]);
    // Announced as the long-poll would have announced it, so the cleanup behind
    // a vanished draft runs whoever pressed the button.
    expect(last.delta).toEqual({ rows: [], gone: ["a"] });
    expect(gone[gone.length - 1]).toEqual(["a"]);
    expect(readListing()?.map((t) => t.key)).toEqual(["b"]);
    off();
    offGone();
  });

  test("a key nothing is holding still reaches onGone, and repaints nobody", async () => {
    const e = env([], [{ tasks: [row("a")] }]);
    const gone: string[][] = [];
    const offGone = onGone((keys) => gone.push(keys));
    const seen: ListingEvent[] = [];
    const off = subscribeListing((ev) => seen.push(ev), e);
    await settle();
    const painted = seen.length;

    dropListingKeys(["new:/somewhere/else"]);
    expect(gone[gone.length - 1]).toEqual(["new:/somewhere/else"]);
    expect(seen.length).toBe(painted);
    off();
    offGone();
  });

  test("nothing at all for an empty list", async () => {
    const e = env([], [{ tasks: [row("a")] }]);
    const gone: string[][] = [];
    const offGone = onGone((keys) => gone.push(keys));
    const off = subscribeListing(() => {}, e);
    await settle();
    dropListingKeys([]);
    dropListingKeys([""]);
    expect(gone).toEqual([]);
    off();
    offGone();
  });

  test("the drop does NOT age the generation the server's next answer is judged by",
    async () => {
      // A local removal is not news from the server, so a full read that left
      // before it must still land — otherwise the row would be stuck gone until
      // something else moved.
      const e = env([], [{ tasks: [row("a"), row("b")], generation: 5 },
                         { tasks: [row("b")], generation: 5 }]);
      const seen: ListingEvent[] = [];
      const off = subscribeListing((ev) => seen.push(ev), e);
      await settle();
      dropListingKeys(["a"]);
      refreshListing();
      await settle();
      expect(seen[seen.length - 1].rows.map((t) => t.key)).toEqual(["b"]);
      off();
    });
});
