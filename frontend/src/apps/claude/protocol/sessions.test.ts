// The recent list's `null` vs `[]` semantics and which changes are worth
// emitting for.
//
// The feed itself lives in `shell/tasksPulse` (one `tasks.listing` subscription
// on the events bus for the whole document), so these drive it through
// `subscribeTasks` exactly as the list does — the `env` seam is a scripted bus
// subscription — and every assertion below is about the contract this module
// still owns: what a subscription emits, and which changes are worth emitting
// for.
import { beforeEach, describe, expect, test } from "bun:test";

import type { Task } from "@platform/lib/api";
import { resetListingFeedForTests } from "@shell/tasksPulse";
import { changeIsHere, subscribeTasks, type RecentEnv } from "./sessions";

// The feed is MODULE state — one per document in the app, and so one per `bun
// test` process here. A case that hands over its own scripted `env` needs the
// last one's rows gone, or it would be replayed them on subscribe.
beforeEach(() => {
  resetListingFeedForTests();
});

const row = (key: string): Task =>
  ({
    key,
    task_id: key.toUpperCase(),
    project: "/proj",
    target: "/proj/app.py",
    session_id: key,
    title: key,
  }) as Task;

type Pushed = { snap?: unknown; delta?: unknown; fail?: true };

/** A scripted bus subscription: the frames in `script` are pushed, in order,
 *  the moment the feed subscribes; `push` adds more by hand. */
function env(script: Pushed[]): RecentEnv & { push(f: Pushed): void; subscribed(): number; resyncs(): number } {
  let cb: ((s: unknown, d: unknown, m: Record<string, unknown>) => void) | null = null;
  let subscribed = 0;
  let resyncs = 0;
  const deliver = (f: Pushed) => {
    if (!cb) return;
    if (f.fail) cb(null, null, { error: "offline", status: 500 });
    else if (f.snap !== undefined) cb(f.snap, null, { gen: 1 });
    else if (f.delta !== undefined) cb(null, f.delta, { gen: 2 });
  };
  return {
    subscribe: (_topic, _params, fn) => {
      subscribed += 1;
      cb = fn as typeof cb;
      for (const f of script) deliver(f);
      return () => {
        subscribed -= 1;
        if (cb === fn) cb = null;
      };
    },
    resync: () => {
      resyncs += 1;
    },
    push: deliver,
    subscribed: () => subscribed,
    resyncs: () => resyncs,
  };
}

const settle = async () => {
  for (let i = 0; i < 40; i++) await Promise.resolve();
};

describe("changeIsHere (T:18384)", () => {
  test("this folder, or a folder above this file", () => {
    expect(changeIsHere("/proj", "/proj/app.py")).toBe(true);
    expect(changeIsHere("/proj/app.py", "/proj/app.py")).toBe(true);
    expect(changeIsHere("/other", "/proj/app.py")).toBe(false);
    // "/pro" is not a parent of "/proj/app.py" — the separator matters.
    expect(changeIsHere("/pro", "/proj/app.py")).toBe(false);
    expect(changeIsHere(null, "/proj/app.py")).toBe(false);
  });
});

describe("subscribeTasks", () => {
  test("`null` first (the skeleton), then the rows", async () => {
    const seen: (Task[] | null)[] = [];
    const e = env([{ snap: { tasks: [row("a"), row("b")] } }]);
    const off = subscribeTasks("/proj/app.py", (r) => seen.push(r), e);
    await settle();
    off();
    expect(seen[0]).toBeNull();
    expect(seen[1]?.map((t) => t.key)).toEqual(["a", "b"]);
    // `null` is emitted ONCE — a later snapshot repaints in place (T:18411).
    expect(seen.filter((s) => s === null).length).toBe(1);
  });

  test("no target ⇒ an empty list and no watch at all", async () => {
    const seen: (Task[] | null)[] = [];
    const e = env([]);
    subscribeTasks(null, (r) => seen.push(r), e)();
    await settle();
    expect(seen).toEqual([null, []]);
    expect(e.subscribed()).toBe(0);
  });

  // A FAILED FRAME EMITS NOTHING (Akshil QA, 2026-09-16: "the list goes blank").
  // It used to emit `[]` — the feed's own answer, since a failure makes it
  // forget its listing — and `[]` is the count the Recent block's visibility is
  // decided on (`ui/lists-visibility.isFilled`), so one dropped read took the
  // heading, the tab and every row off screen until the next one landed.
  test("a FAILED frame keeps the rows already up — it is not an empty list", async () => {
    const seen: (Task[] | null)[] = [];
    const e = env([{ snap: { tasks: [row("a"), row("b")] } }]);
    const off = subscribeTasks("/proj/app.py", (r) => seen.push(r), e);
    await settle();
    expect(seen[seen.length - 1]?.map((t) => t.key)).toEqual(["a", "b"]);
    e.push({ fail: true });
    await settle();
    off();
    // No `[]` ever reached the list, and the rows it is drawing are still the
    // ones the last GOOD snapshot gave it.
    expect(seen.some((r) => Array.isArray(r) && r.length === 0)).toBe(false);
    expect(seen[seen.length - 1]?.map((t) => t.key)).toEqual(["a", "b"]);
  });

  test("…and a list that never had rows keeps its SKELETON, not \"no chats\"", async () => {
    const seen: (Task[] | null)[] = [];
    const off = subscribeTasks("/proj/app.py", (r) => seen.push(r), env([{ fail: true }]));
    await settle();
    off();
    // The opening `null` and nothing after it: "we could not read" is not the
    // same news as "this folder has no chats", and only one of the two is true.
    expect(seen).toEqual([null]);
  });

  test("a snapshot with no `tasks` at all is an empty list", async () => {
    const seen: (Task[] | null)[] = [];
    const off = subscribeTasks("/proj/app.py", (r) => seen.push(r), env([{ snap: {} }]));
    await settle();
    off();
    expect(seen[seen.length - 1]).toEqual([]);
  });

  test("a row in this folder repaints the list; one elsewhere does not", async () => {
    const e = env([
      { snap: { tasks: [row("a")] } },
      { delta: { rows: [{ project: "/elsewhere" }] } },
      { delta: { rows: [{ project: "/proj" }] } },
    ]);
    let reads = 0;
    const off = subscribeTasks(
      "/proj/app.py",
      (r) => {
        if (r) reads++;
      },
      e,
    );
    await settle();
    off();
    expect(reads).toBe(2); // the snapshot, plus the one the /proj row caused
  });

  test("a `gone` key only counts when the list was showing it (T:18352)", async () => {
    const e = env([
      { snap: { tasks: [row("a")] } },
      { delta: { gone: ["nope"] } },
      { delta: { gone: ["a"] } },
    ]);
    let reads = 0;
    const off = subscribeTasks(
      "/proj/app.py",
      (r) => {
        if (r) reads++;
      },
      e,
    );
    await settle();
    off();
    expect(reads).toBe(2);
  });

  test("unsubscribing drops the bus subscription", async () => {
    const e = env([{ snap: { tasks: [] } }]);
    const off = subscribeTasks("/proj/app.py", () => {}, e);
    await settle();
    expect(e.subscribed()).toBe(1);
    off();
    expect(e.subscribed()).toBe(0);
  });

  test("rows with no key are dropped rather than rendered", async () => {
    const seen: (Task[] | null)[] = [];
    const off = subscribeTasks(
      "/proj/app.py",
      (r) => seen.push(r),
      env([{ snap: { tasks: [row("a"), null, { title: "no key" }] } }]),
    );
    await settle();
    off();
    expect(seen[seen.length - 1]?.map((t) => t.key)).toEqual(["a"]);
  });
});

// ── R3-1: the list does not wait for the long-poll to notice ────────────────
//
// A brand-new session becomes a row only once the CLI has written the first
// lines of its transcript and the server's watcher has seen them — SECONDS
// after the snapshot this mount is handed. The two looks below ask the bus for
// a fresh snapshot across exactly that window: "new task from Home → Back: not
// in Recent chats, needed a refresh" (owner, R3-1).
describe("subscribeTasks — the push side (R3-1)", () => {
  /** The env, plus a hand-driven poke channel and retry clock. */
  function pushEnv(listings: unknown[]) {
    const e = env(listings.length ? [{ snap: listings[0] }] : []);
    const fired: Array<() => void> = [];
    const timers: Array<{ ms: number; fn: () => void; cancelled: boolean }> = [];
    const wrapped: RecentEnv & { push: typeof e.push; resyncs: typeof e.resyncs } = {
      ...e,
      pokes: (fn) => {
        fired.push(fn);
        return () => {
          const i = fired.indexOf(fn);
          if (i >= 0) fired.splice(i, 1);
        };
      },
      after: (ms, fn) => {
        const t = { ms, fn, cancelled: false };
        timers.push(t);
        return () => {
          t.cancelled = true;
        };
      },
    };
    return { env: wrapped, poke: () => fired.forEach((f) => f()), fired, timers };
  }

  test("a poke asks the bus for a fresh snapshot, and the snapshot repaints in place", async () => {
    const seen: (Task[] | null)[] = [];
    const p = pushEnv([{ tasks: [row("a")] }]);
    const off = subscribeTasks("/proj/app.py", (r) => seen.push(r), p.env);
    await settle();
    expect(seen[seen.length - 1]?.map((t) => t.key)).toEqual(["a"]);
    // The turn that just ended, announced by this document's own controller —
    // or by any other document's, through the activity stamp.
    p.poke();
    await settle();
    expect(p.env.resyncs()).toBe(1);
    p.env.push({ snap: { tasks: [row("a"), row("b")] } });
    off();
    expect(seen[seen.length - 1]?.map((t) => t.key)).toEqual(["a", "b"]);
    // The skeleton is still spent exactly once: a snapshot over a drawn list
    // repaints in place (T:18411).
    expect(seen.filter((s) => s === null).length).toBe(1);
  });

  test("two more looks a few seconds apart cover the CLI's transcript write", async () => {
    const p = pushEnv([{ tasks: [] }]);
    const seen: (Task[] | null)[] = [];
    // `coverWrite` — T's `leftLive`. The looks are for a chat left MID-TURN.
    const off = subscribeTasks(
      "/proj/app.py",
      (r) => seen.push(r),
      p.env,
      true,
    );
    await settle();
    // T's own schedule, from its Back handler (T:13066).
    expect(p.timers.map((t) => t.ms)).toEqual([2500, 6000]);
    p.timers[0].fn();
    await settle();
    expect(p.env.resyncs()).toBe(1);
    p.env.push({ snap: { tasks: [row("a")] } });
    off();
    expect(seen[seen.length - 1]?.map((t) => t.key)).toEqual(["a"]);
  });

  test("A COLD LANDING SCHEDULES NEITHER (T:13066, P4-21)", async () => {
    // T gates them on `leftLive` because their whole purpose is covering the
    // CLI's first transcript write for a chat abandoned mid-turn. PR4 shipped
    // them unconditionally, so every cold landing boot spent two extra
    // listing reads for a write that had already happened.
    const p = pushEnv([{ tasks: [row("a")] }]);
    const seen: (Task[] | null)[] = [];
    const off = subscribeTasks("/proj/app.py", (r) => seen.push(r), p.env);
    await settle();
    expect(p.timers.map((t) => t.ms)).toEqual([]);
    // One snapshot, and the rows are up: the list is what it honestly is.
    expect(seen[seen.length - 1]?.map((t) => t.key)).toEqual(["a"]);
    off();
  });

  test("unsubscribing takes the listeners AND the pending retries with it", async () => {
    const p = pushEnv([{ tasks: [] }]);
    const off = subscribeTasks("/proj/app.py", () => {}, p.env);
    await settle();
    expect(p.fired.length).toBe(1);
    off();
    // A listener on `window` and a pending timer both outlive this closure
    // otherwise, and six card mounts leak six.
    expect(p.fired.length).toBe(0);
    expect(p.timers.every((t) => t.cancelled)).toBe(true);
  });

  test("a poke after unsubscribing asks for nothing", async () => {
    const p = pushEnv([{ tasks: [] }]);
    const seen: (Task[] | null)[] = [];
    const off = subscribeTasks("/proj/app.py", (r) => seen.push(r), p.env);
    await settle();
    const fn = p.fired[0];
    off();
    fn(); // a handler the host has not detached yet, mid-teardown
    await settle();
    // Nothing asked for after the teardown: `stopped` guards the resync.
    expect(p.env.resyncs()).toBe(0);
    expect(seen.filter((s) => s !== null).length).toBe(1);
  });
});
