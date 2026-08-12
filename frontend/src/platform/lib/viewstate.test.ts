// The viewstate map's one non-trivial operation: dropping a param that the app
// no longer honours from every entry at once. Split out as a pure function over
// the map because there is no localStorage in a bun test (and no DOM at all) —
// the storage wrapper around it is two lines that cannot be got wrong, the
// rewriting of every folder's query string is not.
import { describe, expect, test } from "bun:test";
import { stripParam } from "@platform/lib/viewstate";

describe("stripParam", () => {
  test("removes the param and keeps the rest of the entry", () => {
    // The case the pane-width purge is for: a folder that also remembers a
    // sort, which is per-folder on purpose and must survive untouched.
    expect(stripParam({ "/a": "?sort=size&order=desc&panew=0.42" }, "panew")).toEqual({
      "/a": "?sort=size&order=desc",
    });
  });

  test("an entry that held nothing else is dropped, not left as a bare ?", () => {
    // setViewState stores "" as absence, so a leftover "?" (or "") would be an
    // entry that means nothing and keeps the map growing.
    expect(stripParam({ "/a": "?panew=0.42" }, "panew")).toEqual({});
  });

  test("every entry is visited, not just the first match", () => {
    expect(
      stripParam({ "/a": "?panew=0.3", "/b": "?sort=name", "/c": "?panew=0.7&sort=size" }, "panew")
    ).toEqual({ "/b": "?sort=name", "/c": "?sort=size" });
  });

  test("a map with nothing to strip comes back equal", () => {
    const map = { "/a": "?sort=name&order=asc" };
    expect(stripParam(map, "panew")).toEqual(map);
  });

  test("the input map is not mutated", () => {
    // The wrapper reads, strips, and writes back; a mutating strip would make
    // the "did anything change?" question unanswerable.
    const map = { "/a": "?panew=0.42" };
    stripParam(map, "panew");
    expect(map).toEqual({ "/a": "?panew=0.42" });
  });

  test("repeated occurrences of the param all go", () => {
    expect(stripParam({ "/a": "?panew=0.3&panew=0.7&sort=name" }, "panew")).toEqual({
      "/a": "?sort=name",
    });
  });

  test("an empty map is fine", () => {
    expect(stripParam({}, "panew")).toEqual({});
  });
});
