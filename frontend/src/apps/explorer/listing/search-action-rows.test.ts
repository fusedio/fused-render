import { describe, expect, test } from "bun:test";
import { searchAffordance } from "@apps/explorer/listing/search-action-rows";
import type { TypedAddress } from "@apps/explorer/listing/useTypedPathAddress";

const IDLE: TypedAddress = { status: "idle" };
const MISSING: TypedAddress = { status: "missing" };
const EXISTS: TypedAddress = { status: "exists", path: "/a/b", is_dir: true };

describe("searchAffordance", () => {
  test("nothing while idle (not searching)", () => {
    expect(searchAffordance("report", false, IDLE, false, false, false, false)).toEqual({
      notice: null,
      action: null,
    });
  });

  test("nothing for an empty query even if searching were somehow true", () => {
    expect(searchAffordance("   ", false, IDLE, true, false, false, false)).toEqual({
      notice: null,
      action: null,
    });
  });

  test("pristine wins over everything else — checked before searching/gated/path-shape", () => {
    expect(searchAffordance("/Users/iamsdas", true, EXISTS, true, false, true, false)).toEqual({
      notice: null,
      action: null,
    });
    expect(searchAffordance("report", false, IDLE, true, true, true, false)).toEqual({
      notice: null,
      action: null,
    });
  });

  // SPEC-omnibox-search-affordance.md correction (2026-09-10, defect 1): an
  // UNGATED non-path query is already answering live below — the row would
  // read as "nothing has happened yet" over a box that has already acted.
  test("an ungated word or glob offers nothing — live hits are already the answer", () => {
    expect(searchAffordance("report", false, IDLE, true, false, false, false)).toEqual({
      notice: null,
      action: null,
    });
    expect(searchAffordance("*/*.json", false, IDLE, true, false, false, false)).toEqual({
      notice: null,
      action: null,
    });
  });

  test("a GATED non-path query offers to commit the query verbatim", () => {
    expect(searchAffordance("readme", false, IDLE, true, true, false, false)).toEqual({
      notice: null,
      action: { label: 'Search this folder for "readme"', query: "readme", commitInPlace: true },
    });
  });

  test("a gated query is trimmed before it's quoted back", () => {
    expect(searchAffordance("  readme  ", false, IDLE, true, true, false, false)).toEqual({
      notice: null,
      action: { label: 'Search this folder for "readme"', query: "readme", commitInPlace: true },
    });
  });

  // FINDING 2 (code review, 2026-09-10): a GATED query escapes to a
  // genuinely different folder than the one on screen — the label has to
  // name THAT folder (`folderToOpen`, enter-prompt.ts), reusing the retired
  // banner's own wording, not claim "this folder".
  test("a GATED query naming a different folder is labelled with that folder, not 'this folder'", () => {
    expect(searchAffordance("~/other/*.py", false, IDLE, true, true, false, false)).toEqual({
      notice: null,
      action: {
        label: "Press Enter to open ~/other and search",
        query: "~/other/*.py",
        commitInPlace: true,
      },
    });
  });

  test("a GATED query with nothing left to name after folderToOpen falls back to the generic label", () => {
    expect(searchAffordance("~", false, IDLE, true, true, false, false)).toEqual({
      notice: null,
      action: { label: 'Search this folder for "~"', query: "~", commitInPlace: true },
    });
  });

  test("a path-shaped query that resolves offers nothing — Enter already opens it", () => {
    expect(searchAffordance("~/Work", true, EXISTS, true, true, false, false)).toEqual({
      notice: null,
      action: null,
    });
  });

  test("a path-shaped query still checking offers nothing yet", () => {
    expect(
      searchAffordance("~/Work", true, { status: "checking" }, true, true, false, false),
    ).toEqual({
      notice: null,
      action: null,
    });
  });

  test("a path-shaped query that does not resolve gets the not-found notice plus a search offer on its basename", () => {
    expect(searchAffordance("~/Work/nope", true, MISSING, true, true, false, false)).toEqual({
      notice: "No such file or folder: nope",
      action: { label: 'Search this folder for "nope"', query: "nope", commitInPlace: false },
    });
  });

  // The missing-path branch runs unconditionally on isPathQuery/typedAddress
  // — never asks the index at all (useListingSearch.ts suppresses it by
  // design) — so it fires the same way whether or not `escapesFsPath` would
  // call this particular path "gated".
  test("the not-found offer is unaffected by `gated` — a path query never asks the index either way", () => {
    expect(searchAffordance("~/Work/nope", true, MISSING, true, false, false, false)).toEqual({
      notice: "No such file or folder: nope",
      action: { label: 'Search this folder for "nope"', query: "nope", commitInPlace: false },
    });
  });

  test("the not-found offer rewrites in place rather than committing the escaping text", () => {
    const result = searchAffordance(
      "/tmp/does-not-exist",
      true,
      MISSING,
      true,
      true,
      false,
      false,
    );
    expect(result.action?.commitInPlace).toBe(false);
    expect(result.action?.query).toBe("does-not-exist");
  });

  // FINDING 3 (code review, 2026-09-10): a live completion for the same text
  // means the query is mid-typed toward something real, not a dead end — the
  // whole not-found row (notice AND its offer) has nothing true left to say.
  describe("hasCompletions suppresses the not-found row entirely", () => {
    test("a missing path WITH a live completion offers nothing", () => {
      expect(searchAffordance("~/Work/Doc", true, MISSING, true, true, false, true)).toEqual({
        notice: null,
        action: null,
      });
    });

    test("a missing path with NO completions still gets the notice and offer", () => {
      expect(searchAffordance("~/Work/zzzz", true, MISSING, true, true, false, false)).toEqual({
        notice: "No such file or folder: zzzz",
        action: { label: 'Search this folder for "zzzz"', query: "zzzz", commitInPlace: false },
      });
    });
  });
});
