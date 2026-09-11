import { describe, expect, it } from "bun:test";
import { indexCaveat, searchCaveat } from "@apps/explorer/listing/index-caveat";
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
    reused: 0,
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

  it("says indexing… for a rescan this app triggered but nobody has seen yet", () => {
    // The gap the old freshness gate used to cover by disqualifying the folder
    // outright: between the rename and the rescan appearing in a status poll,
    // the index still spells the old name and `scanning` is still false. The
    // rows on screen are the ones that are wrong, so the caption has to be up
    // before the poller catches the scan, not after.
    const c = indexCaveat(status({ scanning: false }), false, true)!;
    expect(c.note).toBe("indexing…");
  });

  it("still says nothing once the rescan has landed", () => {
    expect(indexCaveat(status({ scanning: false }), false, false)).toBeNull();
  });

  it("a pending rescan outranks being a generation behind", () => {
    // Both are true after an in-app change; "indexing…" is the one that says
    // something is coming.
    expect(indexCaveat(status({ scanning: false }), true, true)!.note).toBe("indexing…");
  });

  it("says the search failed rather than merely 'not refreshed'", () => {
    // A failed request with rows on screen is a `behind` case too (see
    // `useListingSearch.ts`'s `behind`), but "not refreshed… run it again"
    // promises a plain re-run will catch up, which is false right after one
    // just failed — this caption has to say what actually happened.
    const c = indexCaveat(status({ scanning: false }), true, false, true)!;
    expect(c.note).toBe("search failed");
    expect(c.title).toContain("failed");
  });

  it("a running scan still outranks a failed request", () => {
    expect(indexCaveat(status(), true, false, true)!.note).toBe("indexing…");
  });
});

describe("searchCaveat", () => {
  const state = (over: Partial<Parameters<typeof searchCaveat>[1]> = {}) => ({
    behind: false, pending: false, rescanPending: false, ...over,
  });

  it("says nothing about a query that is merely in flight", () => {
    // Every keystroke leaves the rows answering the previous query for a
    // moment. Calling that "not refreshed — clear the search and run it
    // again" is corpus-staleness language for a 40ms round trip, printed
    // where the 200ms rule deliberately withholds even a spinner.
    expect(searchCaveat(status({ scanning: false }), state({ behind: true, pending: true })))
      .toBeNull();
  });

  it("says it once the rows are stuck", () => {
    expect(searchCaveat(status({ scanning: false }), state({ behind: true }))!.note)
      .toBe("not refreshed");
  });

  it("says indexing… for a rescan this app triggered", () => {
    expect(searchCaveat(status({ scanning: false }), state({ rescanPending: true }))!.note)
      .toBe("indexing…");
  });

  it("keeps quiet when there is nothing to say", () => {
    expect(searchCaveat(status({ scanning: false }), state())).toBeNull();
  });

  it("says the search failed when told the rows are behind a failed request", () => {
    expect(
      searchCaveat(status({ scanning: false }), state({ behind: true, failed: true }))!.note,
    ).toBe("search failed");
  });

  it("defaults to the generic caption when the caller never passes failed", () => {
    // FilesHome.tsx's own search can fail the same way but reports it
    // through its own banner, not this chip — omitting `failed` must not
    // crash and must not silently claim "search failed" on its behalf.
    expect(searchCaveat(status({ scanning: false }), state({ behind: true }))!.note)
      .toBe("not refreshed");
  });
});
