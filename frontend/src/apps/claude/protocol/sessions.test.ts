// The recent list's `null` vs `[]` semantics and the change-poll's loop.
//
// The loop itself moved to `shell/tasksPulse` (one read and one long-poll for
// the whole document), so these drive it through `subscribeTasks` exactly as the
// list does — the `env` seam is the same one, and every assertion below is about
// the contract this module still owns: what a subscription emits, and which
// changes are worth emitting for.
import { beforeEach, describe, expect, test } from "bun:test";

import type { Task } from "@platform/lib/api";
import { resetListingFeedForTests } from "@shell/tasksPulse";
import { changeIsHere, CHANGES_BACKOFF_MS, subscribeTasks, type RecentEnv } from "./sessions";

// The feed is MODULE state — one per document in the app, and so one per `bun
// test` process here. A case that hands over its own scripted `env` needs the
// last one's rows and generation gone, or it would be replayed them on subscribe.
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

/** A change-poll that answers from a script and records its waits. */
function env(script: unknown[], listings: unknown[]): RecentEnv & { urls: string[]; waits: number[] } {
  const urls: string[] = [];
  const waits: number[] = [];
  let i = 0;
  let s = 0;
  return {
    urls,
    waits,
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
      const answer = listings[Math.min(s++, listings.length - 1)];
      if (answer === "boom") return Promise.reject(new Error("no such folder"));
      return Promise.resolve(answer as { tasks?: Task[] });
    },
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
    const e = env([], [{ tasks: [row("a"), row("b")] }]);
    const off = subscribeTasks("/proj/app.py", (r) => seen.push(r), e);
    await settle();
    off();
    expect(seen[0]).toBeNull();
    expect(seen[1]?.map((t) => t.key)).toEqual(["a", "b"]);
    // `null` is emitted ONCE — a re-read repaints in place (T:18411).
    expect(seen.filter((s) => s === null).length).toBe(1);
  });

  test("no target ⇒ an empty list and no watch at all", async () => {
    const seen: (Task[] | null)[] = [];
    const e = env([], []);
    subscribeTasks(null, (r) => seen.push(r), e)();
    await settle();
    expect(seen).toEqual([null, []]);
    expect(e.urls.length).toBe(0);
  });

  test("a failed read is an empty list, never an error UI (T:18469)", async () => {
    const seen: (Task[] | null)[] = [];
    const off = subscribeTasks("/proj/app.py", (r) => seen.push(r), env([], ["boom"]));
    await settle();
    off();
    expect(seen[seen.length - 1]).toEqual([]);
  });

  test("an answer with no `tasks` at all is the same as a failure", async () => {
    const seen: (Task[] | null)[] = [];
    const off = subscribeTasks("/proj/app.py", (r) => seen.push(r), env([], [{}]));
    await settle();
    off();
    expect(seen[seen.length - 1]).toEqual([]);
  });

  test("the first change-poll is a handshake that reads nothing back", async () => {
    let reads = 0;
    const e = env([{ generation: 7, full: true }], [{ tasks: [row("a")] }]);
    const off = subscribeTasks(
      "/proj/app.py",
      () => {
        reads++;
      },
      e,
    );
    await settle();
    off();
    expect(e.urls[0]).toBe("/api/tasks/changes?since=-1&wait=25");
    // The handshake's `full` is NOT acted on, and the next poll asks from 7.
    expect(e.urls[1]).toBe("/api/tasks/changes?since=7&wait=25");
    expect(reads).toBe(2); // null + the one read
  });

  test("a row in this folder re-reads the list; one elsewhere does not", async () => {
    const e = env(
      [
        { generation: 1 },
        { generation: 2, rows: [{ project: "/elsewhere" }] },
        { generation: 3, rows: [{ project: "/proj" }] },
      ],
      [{ tasks: [row("a")] }],
    );
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
    expect(reads).toBe(2); // the first load, plus the one the /proj row caused
  });

  test("a `gone` key only counts when the list was showing it (T:18352)", async () => {
    const e = env(
      [{ generation: 1 }, { generation: 2, gone: ["nope"] }, { generation: 3, gone: ["a"] }],
      [{ tasks: [row("a")] }],
    );
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

  test("a failed change-poll backs off 3 s and carries on", async () => {
    const e = env([{ generation: 1 }, "boom", { generation: 2, full: true }], [{ tasks: [] }]);
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
    expect(e.waits).toContain(CHANGES_BACKOFF_MS);
    expect(reads).toBe(2);
  });

  test("unsubscribing aborts the in-flight long-poll (bugbot #892)", async () => {
    let signal: AbortSignal | undefined;
    const e = env([{ generation: 1 }], [{ tasks: [] }]);
    const wrapped: RecentEnv = {
      ...e,
      fetch: (url, init) => {
        signal = init?.signal;
        return e.fetch(url, init);
      },
    };
    const off = subscribeTasks("/proj/app.py", () => {}, wrapped);
    await settle();
    off();
    expect(signal?.aborted).toBe(true);
  });

  test("a hidden tab sits the long-poll out entirely (T:18369)", async () => {
    const e = env([{ generation: 1 }], [{ tasks: [] }]);
    const off = subscribeTasks("/proj/app.py", () => {}, { ...e, hidden: () => true });
    await settle();
    off();
    expect(e.urls.length).toBe(0);
  });

  test("rows with no key are dropped rather than rendered", async () => {
    const seen: (Task[] | null)[] = [];
    const off = subscribeTasks(
      "/proj/app.py",
      (r) => seen.push(r),
      env([], [{ tasks: [row("a"), null, { title: "no key" }] }]),
    );
    await settle();
    off();
    expect(seen[seen.length - 1]?.map((t) => t.key)).toEqual(["a"]);
  });
});

// ── R3-1: the list does not wait for the long-poll to notice ────────────────
//
// The long-poll's first call is a HANDSHAKE that only learns the current
// generation, so everything that moved before this subscription existed is
// already spent — and a run started and finished while the reader was inside the
// chat is exactly that. Landing then spends ONE read, racing the CLI's own
// transcript write, and when it lost, nothing ever came back: "new task from
// Home → Back: not in Recent chats, needed a refresh" (owner, R3-1).
describe("subscribeTasks — the push side (R3-1)", () => {
  /** The env, plus a hand-driven poke channel and retry clock. */
  function pushEnv(listings: unknown[]) {
    const e = env([], listings);
    const fired: Array<() => void> = [];
    const timers: Array<{ ms: number; fn: () => void; cancelled: boolean }> = [];
    const wrapped: RecentEnv = {
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

  test("a poke re-reads the list", async () => {
    const seen: (Task[] | null)[] = [];
    const p = pushEnv([{ tasks: [row("a")] }, { tasks: [row("a"), row("b")] }]);
    const off = subscribeTasks("/proj/app.py", (r) => seen.push(r), p.env);
    await settle();
    expect(seen[seen.length - 1]?.map((t) => t.key)).toEqual(["a"]);
    // The turn that just ended, announced by this document's own controller —
    // or by any other document's, through the activity stamp.
    p.poke();
    await settle();
    off();
    expect(seen[seen.length - 1]?.map((t) => t.key)).toEqual(["a", "b"]);
    // The skeleton is still spent exactly once: a re-read over a drawn list
    // repaints in place (T:18411).
    expect(seen.filter((s) => s === null).length).toBe(1);
  });

  test("two more looks a few seconds apart cover the CLI's transcript write", async () => {
    const p = pushEnv([{ tasks: [] }, { tasks: [row("a")] }]);
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
    // One read, and the rows are up: the list is what it honestly is.
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

  test("a poke after unsubscribing reads nothing", async () => {
    const p = pushEnv([{ tasks: [] }]);
    const seen: (Task[] | null)[] = [];
    const off = subscribeTasks("/proj/app.py", (r) => seen.push(r), p.env);
    await settle();
    const fn = p.fired[0];
    off();
    fn(); // a handler the host has not detached yet, mid-teardown
    await settle();
    // Nothing painted after the teardown: `stopped` guards the read, and the
    // seat guards the paint.
    expect(seen.filter((s) => s !== null).length).toBe(1);
  });
});
