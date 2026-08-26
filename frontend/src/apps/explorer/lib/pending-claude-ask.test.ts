import { beforeEach, describe, expect, test } from "bun:test";
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
