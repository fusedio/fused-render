import { describe, expect, it } from "bun:test";
import { indexCaveat, withCaveat } from "@apps/explorer/listing/index-caveat";
import type { IndexStatus } from "@platform/lib/api";

function status(over: Partial<IndexStatus> = {}): IndexStatus {
  return {
    scanning: true,
    has_index: true,
    files_indexed: 1000,
    last_completed_at: 100,
    running: true,
    run_id: "r",
    root: "/Users/x",
    phase: "scanning (incremental)",
    dirs: 10,
    files: 4321,
    error: null,
    ...over,
  };
}

describe("indexCaveat", () => {
  it("says nothing when no scan is running", () => {
    expect(indexCaveat(status({ scanning: false }))).toBeNull();
    expect(indexCaveat(null)).toBeNull();
  });

  it("warns about staleness when an index already exists", () => {
    const c = indexCaveat(status())!;
    expect(c.note).toBe("indexing…");
    expect(c.title).toContain("last completed index");
  });

  it("reports progress when there is no index yet", () => {
    const c = indexCaveat(status({ has_index: false }))!;
    // the walk is answering here, so this is progress, not a staleness warning
    expect(c.note).toContain("building index…");
    expect(c.note).toContain("4,321");
    expect(c.title).toContain("searched live");
  });

  it("says results are a generation behind when nothing is running", () => {
    // The deal the search makes when it refuses to refetch mid-read
    // (listing/revalidate): stale is fine, silently stale is not.
    const c = indexCaveat(status({ scanning: false }), true)!;
    expect(c.note).toBe("not refreshed");
    expect(c.title).toContain("clear the search");
    // ...and with no status at all, which is the pre-first-poll state.
    expect(indexCaveat(null, true)!.note).toBe("not refreshed");
  });

  it("prefers the running-scan message, which already implies the same thing", () => {
    expect(indexCaveat(status(), true)!.note).toBe("indexing…");
  });
});

describe("withCaveat", () => {
  it("keeps both facts in one chip", () => {
    expect(withCaveat("62 matches", indexCaveat(status()))).toBe("62 matches · indexing…");
  });

  it("stands alone when there is no count yet", () => {
    expect(withCaveat(null, indexCaveat(status()))).toBe("indexing…");
  });

  it("leaves the count untouched when nothing is scanning", () => {
    expect(withCaveat("62 matches", null)).toBe("62 matches");
    expect(withCaveat(null, null)).toBeNull();
  });
});
