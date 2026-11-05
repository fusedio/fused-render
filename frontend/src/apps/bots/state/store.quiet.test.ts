// THE POLL OFF THE BOTS PAGE (store `pollOnce`'s `page` guard, `feedBotQuietly`;
// shell/claude-rail BotPeek, 2026-11-05): with no `.bots-page` mounted the
// poll merges the rows and the events — the rail's bot card reads them — and
// does nothing the page would: no landing on the bare door (no selection),
// no `open` call, no toast. The quiet feed counts its readers and is held by
// the first, released by the last.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, describe, expect, test } from "bun:test";

const { pollOnce, feedBotQuietly, quietFeedReaders, getState, eventsOf } = await import("./store");
const { botsMounted } = await import("../lib/root");

const realFetch = globalThis.fetch;
/** Every URL the poll asks, with what the worker answers it. */
const calls: string[] = [];
function answer(body: unknown): void {
  (globalThis as { fetch: unknown }).fetch = (url: string | URL) => {
    calls.push(String(url));
    return Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }));
  };
}

const reply = {
  ts: 1,
  usage: null,
  imessage: null,
  bots: [
    {
      id: "b1",
      name: "Helper",
      status: "idle",
      seq: 2,
      news_seq: 2,
      seen_seq: null,
      pinned: true,
      events: [
        { seq: 1, ts: 10, role: "user", text: "hi" },
        { seq: 2, ts: 11, role: "done", text: "hello" },
      ],
    },
  ],
};

beforeEach(() => {
  calls.length = 0;
});
afterEach(() => {
  (globalThis as { fetch: unknown }).fetch = realFetch;
});

describe("the bots store off the page", () => {
  test("pollOnce merges rows and events, selects nothing and sends no open", async () => {
    expect(botsMounted()).toBe(false);
    answer(reply);
    await pollOnce();
    expect(getState().bots.map((b) => b.id)).toEqual(["b1"]);
    expect(eventsOf("b1").map((e) => e.seq)).toEqual([1, 2]);
    // The page would land the bare door on the one bot; off it, nothing is
    // selected and nothing but the poll itself was asked of the worker.
    expect(getState().sel).toBeNull();
    expect(calls).toHaveLength(1);
    expect(calls[0]).toContain("/api/bots?");
    expect(calls[0]).toContain("shot_for=&fast=0");
  });

  test("the quiet feed is held by its readers and released by the last", async () => {
    answer(reply);
    expect(quietFeedReaders()).toBe(0);
    const a = feedBotQuietly();
    const b = feedBotQuietly();
    expect(quietFeedReaders()).toBe(2);
    a();
    a();
    expect(quietFeedReaders()).toBe(1);
    b();
    expect(quietFeedReaders()).toBe(0);
  });
});
