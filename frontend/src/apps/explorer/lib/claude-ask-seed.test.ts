import { describe, expect, test } from "bun:test";
import { resolveClaudeAskSeed, type ClaudeAskCache } from "@apps/explorer/lib/claude-ask-seed";

function refs(seed: string | null = null) {
  const pending = { current: seed };
  const cache: { current: ClaudeAskCache | null } = { current: null };
  return { pending, cache };
}

describe("resolveClaudeAskSeed", () => {
  test("delivers a pending seed on the first call for a key, then clears it", () => {
    const { pending, cache } = refs("fix this merge conflict");
    expect(resolveClaudeAskSeed(pending, cache, "file-a")).toBe("fix this merge conflict");
    expect(pending.current).toBeNull();
  });

  test("a stray re-render for the SAME key repeats the same answer, not null", () => {
    const { pending, cache } = refs("fix this");
    resolveClaudeAskSeed(pending, cache, "file-a");
    // Nothing new arrived; some unrelated state changed and the caller asks again.
    expect(resolveClaudeAskSeed(pending, cache, "file-a")).toBe("fix this");
    expect(resolveClaudeAskSeed(pending, cache, "file-a")).toBe("fix this");
  });

  test("THE DEFECT THIS PINS: navigating to a different key never re-delivers a stale seed", () => {
    const { pending, cache } = refs("branch main failed to push");
    // Delivered once, for the file this error was about.
    expect(resolveClaudeAskSeed(pending, cache, "file-a")).toBe("branch main failed to push");
    // The sidebar stays open on claude; the user opens an unrelated file.
    // No new ask arrived (pending is already null) — the stale text must NOT
    // reappear for the new key.
    expect(resolveClaudeAskSeed(pending, cache, "file-b")).toBeNull();
    // And it stays gone on further re-renders of file-b.
    expect(resolveClaudeAskSeed(pending, cache, "file-b")).toBeNull();
  });

  test("a SECOND ask on the same key is delivered, not swallowed by the cache", () => {
    const { pending, cache } = refs("first error");
    expect(resolveClaudeAskSeed(pending, cache, "file-a")).toBe("first error");
    // A second "Fix with AI" click on the same file/folder while still open.
    pending.current = "second error";
    expect(resolveClaudeAskSeed(pending, cache, "file-a")).toBe("second error");
    expect(pending.current).toBeNull();
  });

  test("no seed ever pending resolves to null and stays cached as null", () => {
    const { pending, cache } = refs(null);
    expect(resolveClaudeAskSeed(pending, cache, "file-a")).toBeNull();
    expect(resolveClaudeAskSeed(pending, cache, "file-a")).toBeNull();
  });
});
