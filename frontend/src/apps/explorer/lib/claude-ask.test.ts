import { describe, expect, test } from "bun:test";
import {
  claudeEntryReady,
  resolveClaudeAskRoute,
  takeClaudeAsk,
} from "@apps/explorer/lib/claude-ask";

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

describe("claudeEntryReady", () => {
  test("a real, settled claude entry is ready", () => {
    expect(claudeEntryReady({ mode: "claude" }, false)).toBe(true);
  });

  test("a PENDING claude entry is not ready — review #804 round 3 finding 3's root cause", () => {
    // Answering true here is exactly what let a seed get stored for a target
    // that was never actually going to pull it: the gate could still deny.
    expect(claudeEntryReady({ mode: "claude" }, true)).toBe(false);
  });

  test("no entry at all is not ready", () => {
    expect(claudeEntryReady(null, false)).toBe(false);
    expect(claudeEntryReady(undefined, false)).toBe(false);
  });

  test("an entry for a different mode is not ready", () => {
    expect(claudeEntryReady({ mode: "git" }, false)).toBe(false);
  });
});

describe("resolveClaudeAskRoute", () => {
  test("splitCapable + sideReady -> the sidebar", () => {
    expect(resolveClaudeAskRoute({ splitCapable: true, sideReady: true, contentReady: false }))
      .toBe("side");
  });

  test("splitCapable but the sidebar isn't ready -> nowhere, even if content claims readiness", () => {
    // `contentReady` is irrelevant on a splitCapable surface — there is no
    // content-mode route there, only the split.
    expect(resolveClaudeAskRoute({ splitCapable: true, sideReady: false, contentReady: true }))
      .toBeNull();
  });

  test("not splitCapable + contentReady -> the content pane's own mode switch", () => {
    // review #804 round 3 finding 1: a directory opened at `?_mode=git` has
    // no sidebar (`splitCapable` is false for any directory), but claude is
    // still reachable as an ordinary content mode.
    expect(resolveClaudeAskRoute({ splitCapable: false, sideReady: false, contentReady: true }))
      .toBe("content");
  });

  test("not splitCapable and content isn't ready either -> nowhere", () => {
    expect(resolveClaudeAskRoute({ splitCapable: false, sideReady: true, contentReady: false }))
      .toBeNull();
  });

  test("ready nowhere at all -> null, the honest answer finding 4 requires", () => {
    expect(resolveClaudeAskRoute({ splitCapable: false, sideReady: false, contentReady: false }))
      .toBeNull();
    expect(resolveClaudeAskRoute({ splitCapable: true, sideReady: false, contentReady: false }))
      .toBeNull();
  });
});
