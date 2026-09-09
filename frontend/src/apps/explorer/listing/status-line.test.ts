// One string, one function, one set of decisions: statusLine turns the counts
// Listing.tsx already has lying around into the line under the list. Every
// case in the plan's table is here so the component itself never has to
// decide anything — it renders what this returns.
import { describe, expect, test } from "bun:test";
import { statusLine } from "./status-line";

// A base input every test overrides from, so each case states only the
// fields it is actually about.
const base = {
  total: 0,
  selected: 0,
  selectedBytes: 0,
  folderCount: 0,
  truncated: false,
  searching: false,
  hits: 0,
};

describe("statusLine", () => {
  test("a folder, nothing selected", () => {
    expect(statusLine({ ...base, total: 128 })).toBe("128 items");
  });

  test("a single entry is singular", () => {
    expect(statusLine({ ...base, total: 1 })).toBe("1 item");
  });

  test("a truncated total keeps the '+' and stays plural", () => {
    // "1+" would be a strange singular even if `total` were 1, and a
    // truncated total from the walk is never exactly 1 anyway — the "+"
    // itself is the tell that this is a floor, not a count.
    expect(statusLine({ ...base, total: 1, truncated: true })).toBe("1+ items");
  });

  test("a folder, 3 rows selected", () => {
    expect(
      statusLine({ ...base, total: 128, selected: 3, selectedBytes: 1_468_006 }),
    ).toBe("3 of 128 selected · 1.4 MB");
  });

  test("one row selected", () => {
    // The exact string is whatever formatSize already renders (format.test.ts
    // owns that formatting); this only checks statusLine hands it the right
    // bytes and slots it into the right place.
    expect(
      statusLine({ ...base, total: 128, selected: 1, selectedBytes: 4_096 }),
    ).toBe("1 of 128 selected · 4.0 KB");
  });

  test("a single selected folder shows no size, but does say folder", () => {
    // The size sums FILES only — a lone selected folder has none to sum, and
    // reporting nothing at all would read as "0 bytes", not "unknown". The
    // folder count says why.
    expect(
      statusLine({ ...base, total: 128, selected: 1, folderCount: 1, selectedBytes: 0 }),
    ).toBe("1 of 128 selected · 1 folder");
  });

  test("a truncated listing", () => {
    expect(statusLine({ ...base, total: 1000, truncated: true })).toBe("1,000+ items");
  });

  test("a search running or done, nothing selected", () => {
    expect(statusLine({ ...base, searching: true, hits: 24 })).toBe("24 matches");
  });

  test("a single search hit is singular", () => {
    expect(statusLine({ ...base, searching: true, hits: 1 })).toBe("1 match");
  });

  test("a search with a selection", () => {
    expect(statusLine({ ...base, searching: true, hits: 24, selected: 3 })).toBe(
      "3 of 24 selected",
    );
  });

  test("an empty folder", () => {
    expect(statusLine({ ...base, total: 0 })).toBe("Empty folder");
  });

  test("a mixed selection sums files and counts folders separately", () => {
    expect(
      statusLine({
        ...base,
        total: 128,
        selected: 3,
        folderCount: 1,
        selectedBytes: 1_468_006,
      }),
    ).toBe("3 of 128 selected · 1.4 MB + 1 folder");
  });

  test("several selected folders pluralize", () => {
    expect(
      statusLine({ ...base, total: 128, selected: 2, folderCount: 2, selectedBytes: 0 }),
    ).toBe("2 of 128 selected · 2 folders");
  });

  test("truncated total still labels a selection", () => {
    expect(
      statusLine({ ...base, total: 1000, truncated: true, selected: 5, selectedBytes: 2048 }),
    ).toBe("5 of 1,000+ selected · 2.0 KB");
  });
});
