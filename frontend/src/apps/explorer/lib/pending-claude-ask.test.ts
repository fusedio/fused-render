import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import {
  peekPendingClaudeAsk,
  pendingClaudeAskVersion,
  stageClaudeAsk,
  subscribePendingClaudeAsk,
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

// A "Fix with Claude" no-op when the explorer is already on the target
// folder (Bugbot finding 17b): Listing.tsx and Preview.tsx each pull the
// staged prompt from an effect keyed off `[fsPath, claudeReady]` (or the
// Preview.tsx equivalent). If the explorer is ALREADY at that path with
// Claude ready — the common case, since the user is usually looking at the
// very repo whose card just failed — `navigate()` is a no-op: neither dep
// changes, the effect never re-runs, and the staged prompt expires unseen.
// The fix is a monotonic version + subscription this module bumps on every
// stage, so BOTH consumers can add it to their own dependency array
// alongside fsPath/claudeReady and re-run even when neither of those
// changed.
describe("staging while already mounted at the target path (finding 17b)", () => {
  test("pendingClaudeAskVersion changes on every stage, even to the same path", () => {
    const before = pendingClaudeAskVersion();
    stageClaudeAsk("/Users/me/repo", "first error");
    const afterFirst = pendingClaudeAskVersion();
    expect(afterFirst).not.toBe(before);

    // A SECOND failure on the SAME repo, while the explorer never left it —
    // fsPath and claudeReady are both unchanged, so only the version can
    // signal a consumer to re-check.
    stageClaudeAsk("/Users/me/repo", "second error");
    expect(pendingClaudeAskVersion()).not.toBe(afterFirst);
  });

  test("a subscriber is notified when a stage lands, even for the currently-mounted path", () => {
    let notified = 0;
    const unsubscribe = subscribePendingClaudeAsk(() => {
      notified += 1;
    });
    try {
      stageClaudeAsk("/Users/me/repo", "an error while already here");
      expect(notified).toBe(1);
    } finally {
      unsubscribe();
    }
  });

  test("unsubscribing stops further notifications", () => {
    let notified = 0;
    const unsubscribe = subscribePendingClaudeAsk(() => {
      notified += 1;
    });
    unsubscribe();
    stageClaudeAsk("/Users/me/repo", "after unsubscribe");
    expect(notified).toBe(0);
  });

  test("both Lockstep consumers (independent subscribers) are notified by the same stage", () => {
    // Listing.tsx and Preview.tsx each subscribe on their own — the "Lockstep"
    // contract (Listing.tsx's own comment) is that a folder opened one way
    // must not silently drop the prompt the other way would have shown, so
    // one stage must reach every independent subscriber, not just the first.
    let listingNotified = 0;
    let previewNotified = 0;
    const unsubListing = subscribePendingClaudeAsk(() => {
      listingNotified += 1;
    });
    const unsubPreview = subscribePendingClaudeAsk(() => {
      previewNotified += 1;
    });
    try {
      stageClaudeAsk("/Users/me/repo", "notify both");
      expect(listingNotified).toBe(1);
      expect(previewNotified).toBe(1);
    } finally {
      unsubListing();
      unsubPreview();
    }
  });
});
