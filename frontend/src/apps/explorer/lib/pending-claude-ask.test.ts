import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import {
  peekPendingClaudeAsk,
  stageClaudeAsk,
  takePendingClaudeAsk,
} from "@apps/explorer/lib/pending-claude-ask";

// The store is module-level (there is only ever one pending cross-navigation
// ask, matching one activity card and one button press at a time), so each
// test clears it first rather than relying on running order.
beforeEach(() => {
  takePendingClaudeAsk("/anything");
});

describe("stageClaudeAsk / takePendingClaudeAsk", () => {
  test("a staged ask is delivered to a take for the SAME path", () => {
    stageClaudeAsk("/Users/me/repo", "fix this rebase conflict");
    expect(takePendingClaudeAsk("/Users/me/repo")).toBe("fix this rebase conflict");
  });

  test("read and clear in one step — a second take gets null, not a replay", () => {
    stageClaudeAsk("/Users/me/repo", "fix it");
    expect(takePendingClaudeAsk("/Users/me/repo")).toBe("fix it");
    expect(takePendingClaudeAsk("/Users/me/repo")).toBeNull();
  });

  test("a take for a DIFFERENT path does not consume it", () => {
    stageClaudeAsk("/Users/me/repo", "fix it");
    expect(takePendingClaudeAsk("/Users/me/other")).toBeNull();
    // Still there for its own target — the mismatched take must not have
    // cleared it.
    expect(takePendingClaudeAsk("/Users/me/repo")).toBe("fix it");
  });

  test("nothing staged resolves to null for any path", () => {
    expect(takePendingClaudeAsk("/Users/me/repo")).toBeNull();
  });

  test("a second stage before the first is claimed overwrites the one slot", () => {
    stageClaudeAsk("/Users/me/repo", "first error");
    stageClaudeAsk("/Users/me/repo2", "second error");
    // The first path's ask is gone — there is only ever one pending ask.
    expect(takePendingClaudeAsk("/Users/me/repo")).toBeNull();
    expect(takePendingClaudeAsk("/Users/me/repo2")).toBe("second error");
  });
});

describe("peekPendingClaudeAsk", () => {
  test("reports the pending pair without clearing it", () => {
    stageClaudeAsk("/Users/me/repo", "fix it");
    expect(peekPendingClaudeAsk()).toEqual({ path: "/Users/me/repo", prompt: "fix it" });
    // Peeking must not consume — the real take still finds it.
    expect(takePendingClaudeAsk("/Users/me/repo")).toBe("fix it");
  });

  test("null when nothing is staged", () => {
    expect(peekPendingClaudeAsk()).toBeNull();
  });
});

describe("expiry (finding #13)", () => {
  const realNow = Date.now;
  afterEach(() => {
    Date.now = realNow;
  });

  test("a staged ask delivered well within the TTL still works", () => {
    let now = 1_000_000;
    Date.now = () => now;
    stageClaudeAsk("/Users/me/repo", "fix it");
    now += 5_000; // 5s later — the ordinary navigate-then-mount window
    expect(takePendingClaudeAsk("/Users/me/repo")).toBe("fix it");
  });

  test("an ask nobody claimed in time expires — a later, unrelated visit does not replay it", () => {
    // The exact bug this pins: the navigated-to surface never reached a
    // ready Claude route (gate refused, Claude Code missing), and the user
    // comes back to the SAME folder much later for an unrelated reason —
    // that later visit must not receive the stale ask.
    let now = 1_000_000;
    Date.now = () => now;
    stageClaudeAsk("/Users/me/repo", "stale error from a while ago");
    now += 5 * 60_000; // 5 minutes later, well past the TTL
    expect(takePendingClaudeAsk("/Users/me/repo")).toBeNull();
  });

  test("peek also treats an expired ask as gone", () => {
    let now = 1_000_000;
    Date.now = () => now;
    stageClaudeAsk("/Users/me/repo", "fix it");
    now += 5 * 60_000;
    expect(peekPendingClaudeAsk()).toBeNull();
  });
});
