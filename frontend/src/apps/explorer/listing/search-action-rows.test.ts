import { describe, expect, test } from "bun:test";
import { searchAffordance } from "@apps/explorer/listing/search-action-rows";
import type { TypedAddress } from "@apps/explorer/listing/useTypedPathAddress";

const IDLE: TypedAddress = { status: "idle" };
const MISSING: TypedAddress = { status: "missing" };
const EXISTS: TypedAddress = { status: "exists", path: "/a/b", is_dir: true };

describe("searchAffordance", () => {
  test("nothing while idle (not searching)", () => {
    expect(searchAffordance("report", false, IDLE, false)).toEqual({
      notice: null,
      action: null,
    });
  });

  test("nothing for an empty query even if searching were somehow true", () => {
    expect(searchAffordance("   ", false, IDLE, true)).toEqual({
      notice: null,
      action: null,
    });
  });

  test("a bare word offers to commit the query verbatim", () => {
    expect(searchAffordance("report", false, IDLE, true)).toEqual({
      notice: null,
      action: { query: "report", commitInPlace: true },
    });
  });

  test("a word is trimmed before it's quoted back", () => {
    expect(searchAffordance("  report  ", false, IDLE, true)).toEqual({
      notice: null,
      action: { query: "report", commitInPlace: true },
    });
  });

  test("a path-shaped query that resolves offers nothing — Enter already opens it", () => {
    expect(searchAffordance("~/Work", true, EXISTS, true)).toEqual({
      notice: null,
      action: null,
    });
  });

  test("a path-shaped query still checking offers nothing yet", () => {
    expect(searchAffordance("~/Work", true, { status: "checking" }, true)).toEqual({
      notice: null,
      action: null,
    });
  });

  test("a path-shaped query that does not resolve gets the not-found notice plus a search offer on its basename", () => {
    expect(searchAffordance("~/Work/nope", true, MISSING, true)).toEqual({
      notice: "No such file or folder: nope",
      action: { query: "nope", commitInPlace: false },
    });
  });

  test("the not-found offer rewrites in place rather than committing the escaping text", () => {
    const result = searchAffordance("/tmp/does-not-exist", true, MISSING, true);
    expect(result.action?.commitInPlace).toBe(false);
    expect(result.action?.query).toBe("does-not-exist");
  });
});
