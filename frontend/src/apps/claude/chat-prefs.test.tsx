// WHAT THE CHAT'S ONE PREFS READ MEANS, and above all what a FAILED one means.
// `queue.enabled` defaults OFF, so "we could not ask" has to leave the feature
// off rather than admitting a send through a door that was never confirmed
// open — and the read costs one request per page, however many chats are
// mounted, with exactly one bounded retry.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

const {
  useProjectQueueEnabled,
  queueEnabled,
  publishProjectQueueEnabled,
  resetChatPrefsForTests,
  setPrefsDeadlineForTests,
} = await import("./chat-prefs");

let calls = 0;
let answer: () => Promise<unknown> = async () => ({ queue: { enabled: false } });
const realFetch = globalThis.fetch;
beforeEach(() => {
  // RESET BEFORE, NOT ONLY AFTER. This module is process-global and other
  // suites in the same bun run reach it too — the composer asks for the queue
  // switch, so any file that mounts a chat starts a read against this module.
  // Resetting here bumps the generation, which is what makes a leaked in-flight
  // read from another file unable to write at all.
  resetChatPrefsForTests();
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
  resetChatPrefsForTests();
  (globalThis as { fetch: unknown }).fetch = realFetch;
});

/** One mount of the hook, and every value it has been handed, in order. */
async function probe() {
  const seen: boolean[] = [];
  function Probe() {
    seen.push(useProjectQueueEnabled());
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

test("the queue is off before anyone has asked — the default IS the answer", async () => {
  answer = async () => ({ queue: { enabled: false } });
  const seen = await probe();
  expect(seen[0]).toBe(false); // in flight, and already right
  expect(seen[seen.length - 1]).toBe(false);
  expect(calls).toBe(1);
});

test("an explicit true turns it on", async () => {
  answer = async () => ({ queue: { enabled: true } });
  const seen = await probe();
  expect(seen[seen.length - 1]).toBe(true);
  expect(queueEnabled()).toBe(true);
});

test("a server that predates the field stays off", async () => {
  // `=== true`, never `!== false`: this switch is OPT-IN, so a server with no
  // such field (or a prefs.json never written) must not admit a send through a
  // door nobody confirmed open.
  answer = async () => ({});
  const seen = await probe();
  expect(seen[seen.length - 1]).toBe(false);
});

test("a read that keeps failing leaves the default standing, after ONE retry", async () => {
  answer = async () => {
    throw new Error("offline");
  };
  const seen = await probe();
  expect(seen[seen.length - 1]).toBe(false);
  expect(queueEnabled()).toBe(false);
  expect(calls).toBe(2); // one bounded retry, not a loop
});

test("one dropped request is retried, and the retry's answer is the answer", async () => {
  let first = true;
  answer = async () => {
    if (first) {
      first = false;
      throw new Error("dropped");
    }
    return { queue: { enabled: true } };
  };
  const seen = await probe();
  expect(seen[seen.length - 1]).toBe(true);
  expect(calls).toBe(2);
});

test("after a failed read a later mount can still ask again", async () => {
  answer = async () => {
    throw new Error("offline");
  };
  await probe();
  expect(calls).toBe(2);
  answer = async () => ({ queue: { enabled: true } });
  const seen = await probe();
  expect(seen[seen.length - 1]).toBe(true);
  expect(calls).toBe(3);
});

test("a PUT's own answer is published to every mounted chat without a second GET", async () => {
  answer = async () => ({ queue: { enabled: false } });
  const seen = await probe();
  expect(seen[seen.length - 1]).toBe(false);
  const before = calls;
  act(() => publishProjectQueueEnabled(true));
  expect(seen[seen.length - 1]).toBe(true);
  expect(calls).toBe(before);
});

// ── A HUNG READ IS BOUNDED, and the budget covers BOTH attempts ──
//
// Ported with the deadline itself from the `native_chat_enabled` gate (#1162),
// which this module outlived. Nothing MOUNTS on the queue answer, so the cost
// of a hang here is not a covered pane — it is that `reading` stays pinned to a
// promise that never settles and no later mount can ever ask again.
//
// The budget is shortened in each case for the obvious reason: eight seconds of
// real time per test is not a test anyone runs.

/** Like `probe`, but waits `ms` of real time for the deadline to fire. */
async function probeFor(ms: number) {
  const seen: boolean[] = [];
  function Probe() {
    seen.push(useProjectQueueEnabled());
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

test("a read that HANGS costs one budget and leaves the default standing", async () => {
  setPrefsDeadlineForTests(30);
  // The request the server accepts and never answers — the case the retry and
  // the `catch` both miss, because neither ever runs.
  answer = () => new Promise<unknown>(() => {});
  const seen = await probeFor(60);
  // The queue was off the whole way through: the default IS the answer here.
  expect(seen[seen.length - 1]).toBe(false);
  expect(queueEnabled()).toBe(false);
  // ONE request. The retry is for a REJECTION, which is fast; a hang must not
  // buy a second full window.
  expect(calls).toBe(1);
});

test("a hung read does not pin `reading` — a later mount asks again", async () => {
  setPrefsDeadlineForTests(30);
  answer = () => new Promise<unknown>(() => {});
  await probeFor(60);
  expect(calls).toBe(1);
  // …and now a server that answers. Without the deadline this mount would
  // short-circuit onto the first read's promise and never send anything.
  answer = async () => ({ queue: { enabled: true } });
  const seen = await probe();
  expect(seen[seen.length - 1]).toBe(true);
  expect(calls).toBe(2);
});

test("a read that is merely SLOW is not abandoned in favour of asking again", async () => {
  setPrefsDeadlineForTests(60);
  answer = () =>
    new Promise((done) => setTimeout(() => done({ queue: { enabled: true } }), 20));
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
    return { queue: { enabled: true } };
  };
  const seen = await probeFor(60);
  expect(seen[seen.length - 1]).toBe(true);
  expect(calls).toBe(2);
});
