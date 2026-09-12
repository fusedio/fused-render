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
  publishProjectQueueEnabled,
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
