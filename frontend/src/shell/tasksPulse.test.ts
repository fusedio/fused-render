// The listing feed: one `tasks.listing` subscription for the document, however
// many readers — and the merge and the replay that make that safe to share.
//
// The transport is the events bus (platform/lib/events); here it is a scripted
// `subscribe` the test drives frame by frame, exactly the shape
// `fusedEvents.subscribe` has.
import { afterEach, describe, expect, test } from "bun:test";

import type { Task } from "@platform/lib/api";
import {
  onDraftChange,
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

/** A scripted bus subscription: `snap`/`delta`/`fail` push frames by hand,
 *  every `resync` is counted, and the subscribe/unsubscribe pair is recorded. */
function env() {
  let cb: ((snap: unknown, delta: unknown, meta: Record<string, unknown>) => void) | null = null;
  let pokeFn: (() => void) | null = null;
  const topics: string[] = [];
  let resyncs = 0;
  let subscribed = 0;
  const e: ListingEnv & {
    snap(body: unknown): void;
    delta(body: unknown): void;
    fail(message?: string): void;
    resyncs(): number;
    subscribed(): number;
    poke(): void;
    topics: string[];
  } = {
    topics,
    subscribe: (topic, _params, fn) => {
      topics.push(topic);
      subscribed += 1;
      cb = fn as typeof cb;
      return () => {
        subscribed -= 1;
        if (cb === fn) cb = null;
      };
    },
    resync: () => {
      resyncs += 1;
    },
    pokes: (fn) => {
      pokeFn = fn;
      return () => {
        pokeFn = null;
      };
    },
    snap: (body) => cb?.(body, null, { gen: 1 }),
    delta: (body) => cb?.(null, body, { gen: 2 }),
    fail: (message = "boom") => cb?.(null, null, { error: message, status: 500 }),
    resyncs: () => resyncs,
    subscribed: () => subscribed,
    poke: () => pokeFn?.(),
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
  test("N subscribers, ONE bus subscription", async () => {
    const e = env();
    const seen: ListingEvent[][] = [[], [], []];
    const offs = seen.map((bucket) => subscribeListing((ev) => bucket.push(ev), e));
    e.snap({ tasks: [row("a")] });
    // The cards wall's whole bug in one assertion: twelve mounts used to be
    // twelve of these.
    expect(e.subscribed()).toBe(1);
    expect(e.topics).toEqual(["tasks.listing"]);
    for (const bucket of seen) {
      expect(bucket[bucket.length - 1].rows.map((t) => t.key)).toEqual(["a"]);
    }
    for (const off of offs) off();
    expect(listingFeedLive()).toBe(false);
    expect(e.subscribed()).toBe(0);
  });

  test("a subscriber that throws on a delta does not end the feed", async () => {
    const e = env();
    let threw = 0;
    const seen: ListingEvent[] = [];
    const offBad = subscribeListing((ev) => {
      if (ev.delta && threw === 0) {
        threw += 1;
        throw new Error("bad subscriber");
      }
    }, e);
    const off = subscribeListing((ev) => seen.push(ev), e);
    e.snap({ tasks: [row("a")] });
    e.delta({ rows: [row("b")] });
    e.delta({ rows: [row("c")] });
    expect(threw).toBe(1);
    expect(seen[seen.length - 1].rows.map((t) => t.key).sort()).toEqual(["a", "b", "c"]);
    offBad();
    off();
  });

  test("a late subscriber is replayed the rows it missed, synchronously", async () => {
    const e = env();
    const first = subscribeListing(() => {}, e);
    e.snap({ tasks: [row("a")] });
    const late: ListingEvent[] = [];
    const second = subscribeListing((ev) => late.push(ev), e);
    // No await: a card mounted five minutes in must not wear a skeleton until
    // something happens to change.
    expect(late.length).toBe(1);
    expect(late[0].rows.map((t) => t.key)).toEqual(["a"]);
    expect(late[0].delta).toBeNull();
    expect(e.subscribed()).toBe(1); // and it cost no second subscription
    first();
    second();
  });

  test("the subscription closes with the last subscriber and opens with the next", async () => {
    const e = env();
    const off = subscribeListing(() => {}, e);
    expect(listingFeedLive()).toBe(true);
    off();
    expect(listingFeedLive()).toBe(false);
    expect(e.subscribed()).toBe(0);
    const e2 = env();
    const off2 = subscribeListing(() => {}, e2);
    expect(e2.subscribed()).toBe(1);
    off2();
  });

  test("a delta is MERGED, not re-read: the rows fold in and the order holds", async () => {
    const e = env();
    const seen: ListingEvent[] = [];
    const off = subscribeListing((ev) => seen.push(ev), e);
    e.snap({ tasks: [row("a", 1)] });
    e.delta({ rows: [row("b", 9)] });
    off();
    // The delta cost no resync.
    expect(e.resyncs()).toBe(0);
    const last = seen[seen.length - 1];
    // `last_active` descending, the one ordering promise mergeTaskChanges keeps.
    expect(last.rows.map((t) => t.key)).toEqual(["b", "a"]);
    expect(last.delta?.rows.map((t) => t.key)).toEqual(["b"]);
  });

  test("a `gone` key leaves the rows and reaches onGone", async () => {
    const e = env();
    const gone: string[][] = [];
    const offGone = onGone((keys) => gone.push(keys));
    const seen: ListingEvent[] = [];
    const off = subscribeListing((ev) => seen.push(ev), e);
    e.snap({ tasks: [row("a")] });
    e.delta({ gone: ["a"] });
    off();
    offGone();
    expect(seen[seen.length - 1].rows).toEqual([]);
    expect(gone).toEqual([["a"]]);
  });

  test("a snapshot after a delta is applied in arrival order — a restart is just a snapshot", async () => {
    // Frames ride one socket in order, so there is no generation to guard: a
    // snapshot from a restarted server (counting from one again) simply wins.
    const e = env();
    const seen: ListingEvent[] = [];
    const off = subscribeListing((ev) => seen.push(ev), e);
    e.snap({ tasks: [row("a")], generation: 40 });
    e.delta({ generation: 41, rows: [row("b", 9)] });
    e.snap({ tasks: [row("c")], generation: 1 });
    off();
    expect(seen[seen.length - 1].rows.map((t) => t.key)).toEqual(["c"]);
    expect(readListing()?.map((t) => t.key)).toEqual(["c"]);
  });

  test("a delta with nothing to fold into asks for a snapshot", async () => {
    const e = env();
    const off = subscribeListing(() => {}, e);
    e.delta({ rows: [row("b")] });
    expect(e.resyncs()).toBe(1);
    off();
  });

  test("a FAILED frame does not leave its verdict behind for the next feed", async () => {
    // BUGBOT, 2026-09-15. A failed read forgets the rows, so the replay at the
    // top of `subscribeListing` fell through to `{rows: [], failed: true}` — and
    // the next mount drew "could not be loaded" over an empty list before it had
    // asked anything.
    const bad = env();
    const off = subscribeListing(() => {}, bad);
    bad.fail();
    off();

    const good = env();
    const seen: ListingEvent[] = [];
    const off2 = subscribeListing((ev) => seen.push(ev), good);
    // The very first thing the new subscriber hears must not be a failure.
    expect(seen.map((ev) => ev.failed)).not.toContain(true);
    good.snap({ tasks: [row("a")] });
    off2();
    expect(seen[seen.length - 1].rows.map((t) => t.key)).toEqual(["a"]);
    expect(seen[seen.length - 1].failed).toBe(false);
  });

  test("a burst of pokes is ONE resync", async () => {
    const e = env();
    const off = subscribeListing(() => {}, e);
    e.snap({ tasks: [row("a")] });
    // `focus`, `storage` and `tasks-changed` all fire for one turn ending.
    e.poke();
    e.poke();
    e.poke();
    await settle();
    expect(e.resyncs()).toBe(1);
    off();
  });

  test("a poke after the last unsubscribe asks for nothing", async () => {
    const e = env();
    const off = subscribeListing(() => {}, e);
    off();
    refreshListing();
    await settle();
    expect(e.resyncs()).toBe(0);
  });

  test("a failed frame is `[]` and `failed`, and forgets the rows it was holding", async () => {
    const e = env();
    const seen: ListingEvent[] = [];
    const off = subscribeListing((ev) => seen.push(ev), e);
    e.snap({ tasks: [row("a")] });
    e.fail();
    off();
    const last = seen[seen.length - 1];
    expect(last.rows).toEqual([]);
    expect(last.failed).toBe(true);
    // Never rows kept over a server that has since gone away (#1079).
    expect(readListing()).toBeNull();
  });

  test("there is no floor read and no backoff: the server owns the doubt (D3)", async () => {
    const e = env();
    const off = subscribeListing(() => {}, e);
    e.snap({ tasks: [row("a")] });
    await new Promise((r) => setTimeout(r, 30));
    expect(e.resyncs()).toBe(0);
    off();
  });

  test("a frame after unsubscribing paints nothing", async () => {
    const e = env();
    const seen: ListingEvent[] = [];
    const off = subscribeListing((ev) => seen.push(ev), e);
    e.snap({ tasks: [row("a")] });
    const painted = seen.length;
    off();
    e.snap({ tasks: [row("b")] });
    expect(seen.length).toBe(painted);
  });

  test("rows with no key are dropped from the listing rather than rendered", async () => {
    const e = env();
    const seen: ListingEvent[] = [];
    const off = subscribeListing((ev) => seen.push(ev), e);
    e.snap({ tasks: [row("a"), null, { title: "no key" }] });
    off();
    expect(seen[seen.length - 1].rows.map((t) => t.key)).toEqual(["a"]);
  });

  test("an unchanged row keeps its object across snapshots (memoised rows stand still)", async () => {
    const e = env();
    const seen: ListingEvent[] = [];
    const off = subscribeListing((ev) => seen.push(ev), e);
    e.snap({ tasks: [row("a"), row("b")] });
    const before = seen[seen.length - 1].rows;
    e.snap({ tasks: [row("a"), row("b", 7)] });
    const after = seen[seen.length - 1].rows;
    expect(after[0]).toBe(before[0]);
    expect(after[1]).not.toBe(before[1]);
    off();
  });
});

describe("dropListingKeys", () => {
  test("takes the row off the held listing and announces it as gone", async () => {
    const e = env();
    const gone: string[][] = [];
    const offGone = onGone((keys) => gone.push(keys));
    const seen: ListingEvent[] = [];
    const off = subscribeListing((ev) => seen.push(ev), e);
    e.snap({ tasks: [row("a"), row("b")] });

    dropListingKeys(["a"]);
    const last = seen[seen.length - 1];
    expect(last.rows.map((t) => t.key)).toEqual(["b"]);
    // Announced as the server would have announced it, so the cleanup behind
    // a vanished draft runs whoever pressed the button.
    expect(last.delta).toEqual({ rows: [], gone: ["a"] });
    expect(gone[gone.length - 1]).toEqual(["a"]);
    expect(readListing()?.map((t) => t.key)).toEqual(["b"]);
    off();
    offGone();
  });

  test("a key nothing is holding still reaches onGone, and repaints nobody", async () => {
    const e = env();
    const gone: string[][] = [];
    const offGone = onGone((keys) => gone.push(keys));
    const seen: ListingEvent[] = [];
    const off = subscribeListing((ev) => seen.push(ev), e);
    e.snap({ tasks: [row("a")] });
    const painted = seen.length;

    dropListingKeys(["new:/somewhere/else"]);
    expect(gone[gone.length - 1]).toEqual(["new:/somewhere/else"]);
    expect(seen.length).toBe(painted);
    off();
    offGone();
  });

  test("nothing at all for an empty list", async () => {
    const e = env();
    const gone: string[][] = [];
    const offGone = onGone((keys) => gone.push(keys));
    const off = subscribeListing(() => {}, e);
    e.snap({ tasks: [row("a")] });
    dropListingKeys([]);
    dropListingKeys([""]);
    expect(gone).toEqual([]);
    off();
    offGone();
  });

  test("the drop does NOT stop the server's next snapshot from landing", async () => {
    const e = env();
    const seen: ListingEvent[] = [];
    const off = subscribeListing((ev) => seen.push(ev), e);
    e.snap({ tasks: [row("a"), row("b")], generation: 5 });
    dropListingKeys(["a"]);
    e.snap({ tasks: [row("b")], generation: 5 });
    expect(seen[seen.length - 1].rows.map((t) => t.key)).toEqual(["b"]);
    off();
  });
});

describe("onDraftChange", () => {
  // design §3: the delta says which DRAFT records moved and to what version,
  // so an open composer or task card adopts another tab's save within a second
  // instead of finding out on its next reload.
  const drafts = (changed: { key: string; version: number }[], gone: string[]) =>
    ({ changed, gone });

  test("carries the server's changed/gone straight through", async () => {
    const e = env();
    const seen: Array<[{ key: string; version: number }[], string[]]> = [];
    const offDrafts = onDraftChange((changed, gone) => seen.push([changed, gone]));
    const off = subscribeListing(() => {}, e);
    e.snap({ tasks: [], generation: 1 });
    e.delta({ generation: 2, rows: [], gone: [], drafts: drafts([{ key: "new:/a/x.py", version: 4 }], ["sess-9"]) });
    expect(seen).toEqual([[[{ key: "new:/a/x.py", version: 4 }], ["sess-9"]]]);
    offDrafts();
    off();
  });

  test("…even on a frame whose rows and `gone` are both empty", async () => {
    const e = env();
    const seen: string[] = [];
    const offDrafts = onDraftChange((changed) => seen.push(...changed.map((c) => c.key)));
    const off = subscribeListing(() => {}, e);
    e.snap({ tasks: [], generation: 1 });
    e.delta({ generation: 3, rows: [], gone: [], drafts: drafts([{ key: "sess-1", version: 8 }], []) });
    expect(seen).toEqual(["sess-1"]);
    offDrafts();
    off();
  });

  test("and says nothing at all when the frame carries no drafts key", async () => {
    const e = env();
    let fired = 0;
    const offDrafts = onDraftChange(() => { fired += 1; });
    const off = subscribeListing(() => {}, e);
    e.snap({ tasks: [], generation: 1 });
    e.delta({ generation: 4, rows: [], gone: ["sess-2"] });
    expect(fired).toBe(0);
    offDrafts();
    off();
  });
});

// ---- the queue's rekey is a fold, not a delete and an insert -----------------
// A message waiting in a folder's line is `pending:<entry>`; the beat it is
// dispatched the listing files it under its session and the delta carries the
// session row plus the pending key flagged `gone` in ONE frame
// (routers/tasks.py `_rekeyed_pendings`). Folded with the flag up, that is one
// row changing state — never a frame with both, never a frame with neither.

describe("a dispatched queue row through the feed", () => {
  const waiting = (): Task =>
    ({
      key: "pending:e4",
      task_id: "TASK-052",
      project: "/proj",
      session_id: "",
      status: "queued",
      last_active: 30,
    }) as Task;
  const running = (): Task =>
    ({
      key: "sess-4",
      task_id: "TASK-052",
      project: "/proj",
      session_id: "sess-4",
      status: "in_progress",
      last_active: 35,
    }) as Task;

  /** The flag is MODULE state on `apps/claude/feature-flag` and outlives every
   *  test in this process, so every case here puts it back by hand. */
  const setQueueFlag = async (on: boolean) => {
    const { applyQueueFlagBroadcast, QUEUE_FLAG_BROADCAST_KEY } = await import(
      "@apps/claude/feature-flag"
    );
    applyQueueFlagBroadcast(QUEUE_FLAG_BROADCAST_KEY, JSON.stringify({ on }));
  };

  test("swaps the waiting row for its run in one paint, flag on", async () => {
    await setQueueFlag(true);
    try {
      const e = env();
      const seen: ListingEvent[] = [];
      const off = subscribeListing((ev) => seen.push(ev), e);
      e.snap({ tasks: [waiting()], generation: 1 });
      e.delta({ generation: 2, rows: [running()], gone: ["pending:e4"] });
      off();
      // ONE row for the task, under the session's name, running — and no frame
      // in between held two rows or none.
      for (const ev of seen) {
        expect(ev.rows.filter((t) => t.task_id === "TASK-052").length).toBe(1);
      }
      const last = seen[seen.length - 1];
      expect(last.rows.map((t) => t.key)).toEqual(["sess-4"]);
      expect(last.rows[0].status).toBe("in_progress");
      // The swap rode in one frame, so nothing had to be asked for.
      expect(e.resyncs()).toBe(0);
    } finally {
      await setQueueFlag(false);
    }
  });

  test("holds the row when only the `gone` half arrives, and asks for a snapshot", async () => {
    await setQueueFlag(true);
    try {
      const e = env();
      const seen: ListingEvent[] = [];
      const off = subscribeListing((ev) => seen.push(ev), e);
      e.snap({ tasks: [waiting()], generation: 1 });
      e.delta({ generation: 2, rows: [], gone: ["pending:e4"] });
      // No hole: the row stays, painted as the run it has become…
      const held = seen.find((ev) => ev.delta?.gone.includes("pending:e4"));
      expect(held?.rows.map((t) => t.key)).toEqual(["pending:e4"]);
      expect(held?.rows[0].status).toBe("in_progress");
      // …and a claim is not news, so the whole listing is asked for at once.
      expect(e.resyncs()).toBe(1);
      e.snap({ tasks: [running()], generation: 2 });
      off();
      expect(seen[seen.length - 1].rows.map((t) => t.key)).toEqual(["sess-4"]);
    } finally {
      await setQueueFlag(false);
    }
  });

  test("with the flag off the same frame simply drops the row", async () => {
    const e = env();
    const seen: ListingEvent[] = [];
    const off = subscribeListing((ev) => seen.push(ev), e);
    e.snap({ tasks: [waiting()], generation: 1 });
    e.delta({ generation: 2, rows: [], gone: ["pending:e4"] });
    off();
    expect(seen[seen.length - 1].rows).toEqual([]);
    expect(e.resyncs()).toBe(0);
  });
});
