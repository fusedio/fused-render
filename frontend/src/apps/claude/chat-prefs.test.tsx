// WHAT THE CHAT'S ONE PREFS READ MEANS, and above all what a FAILED one means.
// `chat.recap` defaults ON, so "we could not ask" has to leave the feature on
// rather than quietly dropping it — and the read costs one request per page,
// however many chats are mounted, with exactly one bounded retry.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

const {
  useChatRecapEnabled,
  chatRecapEnabledNow,
  publishChatRecapEnabled,
  resetChatPrefsForTests,
} = await import("./chat-prefs");

let calls = 0;
let answer: () => Promise<unknown> = async () => ({ chat: { recap: true } });
const realFetch = globalThis.fetch;
beforeEach(() => {
  // RESET BEFORE, NOT ONLY AFTER. This module is process-global and other
  // suites in the same bun run reach it too — `ClaudeChat` asks for the recap
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
    seen.push(useChatRecapEnabled());
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

test("the fold is on before anyone has asked — the default IS the answer", async () => {
  answer = async () => ({ chat: { recap: true } });
  const seen = await probe();
  expect(seen[0]).toBe(true); // in flight, and already right
  expect(seen[seen.length - 1]).toBe(true);
  expect(calls).toBe(1);
});

test("an explicit false turns it off", async () => {
  answer = async () => ({ chat: { recap: false } });
  const seen = await probe();
  expect(seen[seen.length - 1]).toBe(false);
  expect(chatRecapEnabledNow()).toBe(false);
});

test("a server that predates the field still shows the fold", async () => {
  // `!== false`, never `=== true`: an older server (or a prefs.json never
  // written) must not silently lose the feature.
  answer = async () => ({});
  const seen = await probe();
  expect(seen[seen.length - 1]).toBe(true);
});

test("a read that keeps failing leaves the default standing, after ONE retry", async () => {
  answer = async () => {
    throw new Error("offline");
  };
  const seen = await probe();
  expect(seen[seen.length - 1]).toBe(true);
  expect(chatRecapEnabledNow()).toBe(true);
  expect(calls).toBe(2); // one bounded retry, not a loop
});

test("one dropped request is retried, and the retry's answer is the answer", async () => {
  let first = true;
  answer = async () => {
    if (first) {
      first = false;
      throw new Error("dropped");
    }
    return { chat: { recap: false } };
  };
  const seen = await probe();
  expect(seen[seen.length - 1]).toBe(false);
  expect(calls).toBe(2);
});

test("after a failed read a later mount can still ask again", async () => {
  answer = async () => {
    throw new Error("offline");
  };
  await probe();
  expect(calls).toBe(2);
  answer = async () => ({ chat: { recap: false } });
  const seen = await probe();
  expect(seen[seen.length - 1]).toBe(false);
  expect(calls).toBe(3);
});

test("a PUT's own answer is published to every mounted chat without a second GET", async () => {
  answer = async () => ({ chat: { recap: true } });
  const seen = await probe();
  expect(seen[seen.length - 1]).toBe(true);
  const before = calls;
  act(() => publishChatRecapEnabled(false));
  expect(seen[seen.length - 1]).toBe(false);
  expect(calls).toBe(before);
});
