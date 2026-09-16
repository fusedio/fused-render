// WHAT A FAILED PREFS GET MEANS. The flag is a tri-state and `null` holds a
// placeholder over every chat embed on the page (ChatMount), so "we could not
// ask" must never be allowed to look like "still asking": the read retries once
// and then settles on `false`, which is the legacy iframe — what these sites
// rendered before the flag existed, and the pref's own default.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

const {
  useNativeChatFlag,
  nativeChatEnabledNow,
  resetNativeChatFlagForTests,
  queueEnabled,
  queueFlagReady,
  rereadFlags,
  publishProjectQueueEnabled,
  setPrefsDeadlineForTests,
  applyQueueFlagBroadcast,
  QUEUE_FLAG_BROADCAST_KEY,
} = await import("./feature-flag");

let calls = 0;
let answer: () => Promise<unknown> = async () => ({ chat: { native: false } });
const realFetch = globalThis.fetch;
beforeEach(() => {
  // RESET BEFORE, NOT ONLY AFTER. This module is process-global and other
  // suites in the same bun run now reach it too — `ClaudeChat` asks for the
  // recap switch on the same one prefs read, so any file that mounts a chat
  // starts a read against this module. One of those landing late used to leave
  // `enabled` already answered here, and the first assertion below is that a
  // fresh mount sees `null`. Resetting here bumps the generation, which is what
  // makes a leaked in-flight read from another file unable to write at all.
  resetNativeChatFlagForTests();
  calls = 0;
  (globalThis as { fetch: unknown }).fetch = async () => {
    calls += 1;
    const body = await answer();
    return { ok: true, json: async () => body } as unknown as Response;
  };
});

const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  resetNativeChatFlagForTests();
  (globalThis as { fetch: unknown }).fetch = realFetch;
});

/** One mount of the hook, and every value it has been handed, in order. */
async function probe() {
  const seen: Array<boolean | null> = [];
  function Probe() {
    seen.push(useNativeChatFlag());
    return null;
  }
  let r!: ReactTestRenderer;
  await act(async () => {
    r = create(<Probe />);
  });
  mounted.push(r);
  // The read is four microtask hops deep (retry, map, catch); one macrotask
  // turn inside `act` covers it and the re-render it causes.
  await act(async () => {
    await new Promise((done) => setTimeout(done, 0));
  });
  return seen;
}

test("a read that keeps failing settles on false — never a permanent null", async () => {
  answer = async () => {
    throw new Error("offline");
  };
  const seen = await probe();
  expect(seen[0]).toBe(null); // in flight: the mount holds a cover
  expect(seen[seen.length - 1]).toBe(false); // …and then legacy, not a forever skeleton
  expect(nativeChatEnabledNow()).toBe(false);
  expect(calls).toBe(2); // ONE bounded retry, not a loop
});

test("one dropped request is retried, and the retry's answer is the answer", async () => {
  let first = true;
  answer = async () => {
    if (first) {
      first = false;
      throw new Error("dropped");
    }
    return { chat: { native: true } };
  };
  const seen = await probe();
  expect(seen[seen.length - 1]).toBe(true);
  expect(calls).toBe(2);
});

test("a read that answers first time costs one request", async () => {
  answer = async () => ({ chat: { native: true } });
  const seen = await probe();
  expect(seen[seen.length - 1]).toBe(true);
  expect(calls).toBe(1);
});

test("after a failed read a later mount can still ask again", async () => {
  answer = async () => {
    throw new Error("offline");
  };
  await probe();
  expect(calls).toBe(2);
  answer = async () => ({ chat: { native: true } });
  const seen = await probe();
  expect(seen[seen.length - 1]).toBe(true);
  expect(calls).toBe(3);
});

// ---- the project queue rides the same one prefs read -------------------------
// `prefs.queue.enabled` (shell/prefs.py `project_queue_enabled`). A reader of
// its own would double the /api/prefs GET every chat embed already makes — six
// mounts on the tasks wall is six needless round trips for one boolean.

test("the queue switch comes off the same GET, and is OPT-IN", () => {
  // Before any read has landed: OFF. Unlike the native flag's tri-state this is
  // an honest answer rather than an absence — off is the pref's own default, and
  // it is exactly what every send did before the feature existed.
  expect(queueEnabled()).toBe(false);
});

test("a server that has never heard of the field reads as off", async () => {
  // `=== true`, the opposite polarity from the recap switch on the same payload:
  // that one defaults ON and is read `!== false`. Getting this one backwards
  // would turn the queue on for every server that predates it.
  answer = async () => ({ chat: { native: true } });
  await probe();
  expect(queueEnabled()).toBe(false);
  expect(calls).toBe(1);
});

test("the one read answers it — no second request", async () => {
  answer = async () => ({ chat: { native: false }, queue: { enabled: true } });
  await probe();
  expect(queueEnabled()).toBe(true);
  expect(calls).toBe(1);
});

test("a Preferences PUT publishes without taking the native flag's answer away", async () => {
  // The publish deliberately does NOT bump the generation: a click about the
  // queue must not put every mounted chat embed back on a skeleton.
  answer = async () => ({ chat: { native: true }, queue: { enabled: false } });
  const seen = await probe();
  expect(seen[seen.length - 1]).toBe(true);
  publishProjectQueueEnabled(true);
  expect(queueEnabled()).toBe(true);
  expect(nativeChatEnabledNow()).toBe(true);
  expect(calls).toBe(1);
});

test("the test reset forgets it too, so a suite starts from 'off'", async () => {
  answer = async () => ({ queue: { enabled: true } });
  await probe();
  expect(queueEnabled()).toBe(true);
  resetNativeChatFlagForTests();
  expect(queueEnabled()).toBe(false);
});

// ── the backstop is a BUDGET, not a per-attempt stopwatch (bugbot, 2026-09-15) ──
//
// It used to wrap each ATTEMPT: a wedged server spent 8 s, was told it had
// failed, and was asked again for another 8 s — sixteen seconds of placeholder
// over every chat embed on the page, twice what this constant names. And the
// first attempt was ABANDONED at the deadline, so a GET that landed a moment
// later — a slow cold start — was thrown away in favour of a fresh request.
//
// The budget is shortened here for the obvious reason: eight seconds of real
// time per case is not a test anyone runs.

/** Like `probe`, but waits `ms` of real time for the deadline to fire. */
async function probeFor(ms: number) {
  const seen: Array<boolean | null> = [];
  function Probe() {
    seen.push(useNativeChatFlag());
    return null;
  }
  let r!: ReactTestRenderer;
  await act(async () => {
    r = create(<Probe />);
  });
  mounted.push(r);
  await act(async () => {
    await new Promise((done) => setTimeout(done, ms));
  });
  return seen;
}

test("A HUNG READ COSTS ONE BUDGET AND IS NOT ASKED AGAIN INSIDE IT", async () => {
  setPrefsDeadlineForTests(30);
  // The request the server accepts and never answers — the case the backstop
  // exists for, and the one that used to buy a second eight-second window.
  answer = () => new Promise<unknown>(() => {});
  // Looked at after ONE budget and a half — where the old code was still `null`
  // with its second attempt in flight, and would not settle until 60 ms.
  const seen = await probeFor(45);
  expect(seen[0]).toBe(null); // in flight: the mount holds a cover
  expect(seen[seen.length - 1]).toBe(false); // …and legacy once the budget is out
  expect(nativeChatEnabledNow()).toBe(false);
  // ONE request. The retry is for a REJECTION, which is fast; a timeout must not
  // buy a second attempt, because that is what doubled the cover.
  expect(calls).toBe(1);
});

test("a read that is merely SLOW is not abandoned in favour of asking again", async () => {
  setPrefsDeadlineForTests(60);
  answer = () =>
    new Promise((done) => setTimeout(() => done({ chat: { native: true } }), 25));
  const seen = await probeFor(120);
  // The answer arrived inside the budget and is the answer.
  expect(seen[seen.length - 1]).toBe(true);
  expect(calls).toBe(1);
});

test("the retry still exists — for a REJECTION, and it spends the same budget", async () => {
  setPrefsDeadlineForTests(200);
  let first = true;
  answer = async () => {
    if (first) {
      first = false;
      throw new Error("dropped");
    }
    return { chat: { native: true } };
  };
  const seen = await probeFor(60);
  expect(seen[seen.length - 1]).toBe(true);
  expect(calls).toBe(2);
});


// ---- the flag is READ before a send asks it (Akshil's QA, 2026-09-16) ---------
// `queueEnabled()` is `false` until the one prefs read lands; a send inside that
// window used to skip the queue's door and start a second run in a busy folder.

test("queueFlagReady starts the read when nothing has, and resolves with the answer in", async () => {
  answer = async () => ({ chat: { native: false }, queue: { enabled: true } });
  expect(queueEnabled()).toBe(false);
  await queueFlagReady();
  expect(queueEnabled()).toBe(true);
  expect(calls).toBe(1);
  // …and a second wait is the same one read, not another request.
  await queueFlagReady();
  expect(calls).toBe(1);
});

test("rereadFlags asks the server again — a tab coming back into view learns the flip", async () => {
  answer = async () => ({ queue: { enabled: false } });
  await queueFlagReady();
  expect(queueEnabled()).toBe(false);
  answer = async () => ({ queue: { enabled: true } });
  await rereadFlags();
  expect(queueEnabled()).toBe(true);
  expect(calls).toBe(2);
});

test("a Settings toggle in ANOTHER tab reaches this one through the storage event", async () => {
  answer = async () => ({ queue: { enabled: false } });
  await queueFlagReady();
  expect(queueEnabled()).toBe(false);
  applyQueueFlagBroadcast(QUEUE_FLAG_BROADCAST_KEY, JSON.stringify({ on: true, at: 1 }));
  expect(queueEnabled()).toBe(true);
  // An unrelated key, or a malformed value, changes nothing.
  applyQueueFlagBroadcast("other", JSON.stringify({ on: false }));
  applyQueueFlagBroadcast(QUEUE_FLAG_BROADCAST_KEY, "nope");
  expect(queueEnabled()).toBe(true);
});

test("publishing a toggle writes the broadcast the other tabs listen for", () => {
  const store = new Map<string, string>();
  const g = globalThis as { localStorage?: unknown };
  const had = g.localStorage;
  g.localStorage = {
    setItem: (k: string, v: string) => store.set(k, v),
    getItem: (k: string) => store.get(k) ?? null,
  };
  try {
    publishProjectQueueEnabled(true);
    const raw = store.get(QUEUE_FLAG_BROADCAST_KEY);
    expect(raw).toBeDefined();
    expect((JSON.parse(String(raw)) as { on: boolean }).on).toBe(true);
  } finally {
    g.localStorage = had;
  }
});


test("a read that FAILED still settles the flag — the next send does not wait 8 s again", async () => {
  setPrefsDeadlineForTests(30);
  answer = async () => {
    throw new Error("refused");
  };
  await queueFlagReady();
  expect(queueEnabled()).toBe(false);
  const before = calls;
  // Bugbot: a failed GET nulled `reading`, so every send re-asked and waited.
  await queueFlagReady();
  expect(calls).toBe(before);
});

test("rereadFlags outranks the read it replaces — a late timeout cannot flip native chat off", async () => {
  setPrefsDeadlineForTests(60);
  let hang = true;
  answer = () =>
    hang
      ? new Promise<unknown>(() => {})
      : Promise.resolve({ chat: { native: true }, queue: { enabled: true } });
  const first = queueFlagReady();
  hang = false;
  await rereadFlags();
  expect(nativeChatEnabledNow()).toBe(true);
  // The hung read now times out; its catch belongs to an older generation.
  await first;
  await new Promise((r) => setTimeout(r, 80));
  expect(nativeChatEnabledNow()).toBe(true);
});
