import { describe, expect, test } from "bun:test";
import { takeClaudeAsk } from "@apps/explorer/lib/claude-ask";

describe("takeClaudeAsk", () => {
  test("returns the pending text and clears it in the same step", () => {
    const pending = { current: "fix this merge conflict" };
    expect(takeClaudeAsk(pending)).toBe("fix this merge conflict");
    expect(pending.current).toBeNull();
  });

  test("a second take (a stray remount with nothing new) gets null, not a replay", () => {
    // THE DEFECT round 2 pins: the round-1 shape ("push into the src, cache
    // by key") replayed the SAME text on a remount that had nothing to do
    // with a new ask. A pull cannot do that: the value is gone the instant it
    // is taken, so any later take — for whatever reason the frame rebooted —
    // gets null.
    const pending = { current: "fix this" };
    expect(takeClaudeAsk(pending)).toBe("fix this");
    expect(takeClaudeAsk(pending)).toBeNull();
    expect(takeClaudeAsk(pending)).toBeNull();
  });

  test("nothing ever pending resolves to null", () => {
    const pending = { current: null };
    expect(takeClaudeAsk(pending)).toBeNull();
  });

  test("a second ask arriving between two takes is delivered on its own take", () => {
    const pending = { current: "first error" };
    expect(takeClaudeAsk(pending)).toBe("first error");
    // A fresh ask arrives (the host's `_fusedClaudeAsk` sets it again).
    pending.current = "second error";
    expect(takeClaudeAsk(pending)).toBe("second error");
    expect(pending.current).toBeNull();
  });
});
