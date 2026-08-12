// The viewstate map's one non-trivial operation: dropping a param that the app
// no longer honours from every entry at once. Split out as a pure function over
// the map because there is no localStorage in a bun test (and no DOM at all) —
// the storage wrapper around it is two lines that cannot be got wrong, the
// rewriting of every folder's query string is not.
import { describe, expect, test } from "bun:test";
import { parseViewStateMap, stripParam } from "@platform/lib/viewstate";

// What comes back out of localStorage is a STRING WRITTEN BY SOMEONE ELSE —
// an older build, a foreign build, a hand-edited devtools value, a half-written
// key. The store's whole type contract (Record<string, string>) rests on this
// one function, and it used to be a bare `JSON.parse(raw) as Record<...>`: a
// cast, which checks nothing.
//
// This stopped being a theoretical tidiness point when the purge moved to
// pane.ts's MODULE INIT. Every other reader is lazy and inside a component, so
// a bad value there degrades one thing; a throw at module init aborts
// evaluation of a module that Listing.tsx and Preview.tsx both import, i.e. the
// whole explorer bundle — a blank app. `Object.entries(null)` throws, and
// `JSON.parse("null")` is exactly how you get a null here.
describe("parseViewStateMap", () => {
  test("reads a stored map", () => {
    expect(parseViewStateMap('{"/a":"?sort=size"}')).toEqual({ "/a": "?sort=size" });
  });

  test("nothing stored", () => {
    expect(parseViewStateMap(null)).toEqual({});
    expect(parseViewStateMap("")).toEqual({});
  });

  test("malformed JSON is an empty store, not a throw", () => {
    expect(parseViewStateMap("{oops")).toEqual({});
    expect(parseViewStateMap("undefined")).toEqual({});
  });

  test("a stored JSON value that is not an object is an empty store", () => {
    // `null` first, because it is the one that used to get through: it is
    // truthy-checked away only when the RAW string is empty, and "null" is not.
    expect(parseViewStateMap("null")).toEqual({});
    expect(parseViewStateMap("[]")).toEqual({});
    expect(parseViewStateMap('["?sort=size"]')).toEqual({});
    expect(parseViewStateMap('"just a string"')).toEqual({});
    expect(parseViewStateMap("42")).toEqual({});
    expect(parseViewStateMap("true")).toEqual({});
  });

  test("non-string values inside a valid object are dropped, entry by entry", () => {
    // The good entries survive: one bad key must not cost a user every folder's
    // sort. A number would reach `new URLSearchParams(42)`, which parses the
    // NUMBER's string form as a query — nonsense, silently.
    expect(
      parseViewStateMap('{"/a":"?sort=size","/b":42,"/c":null,"/d":{"sort":"size"},"/e":"?x=1"}')
    ).toEqual({ "/a": "?sort=size", "/e": "?x=1" });
  });
});

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

  test("a map with nothing to strip comes back AS THE SAME OBJECT", () => {
    // Identity is the signal the caller uses to decide whether to write at all
    // (purgeViewStateParams), so it is load-bearing and not an optimisation:
    // the purge runs at module init on every document load, and a store with
    // nothing left to strip — which is every load after the first — must not
    // be rewritten.
    const map = { "/a": "?sort=name&order=asc" };
    expect(stripParam(map, "panew")).toBe(map);
  });

  test("an entry that does not carry the param keeps its exact string", () => {
    // Not just an equal one: re-serializing through URLSearchParams normalizes
    // the encoding (a space stored as %20 comes back as +). Every reader parses
    // with URLSearchParams so both forms read the same, but a migration that
    // rewrites the whole store on every load would quietly make the STORED
    // format depend on that, for entries it has no business touching.
    const map = { "/My%20Files": "?sort=name%20asc", "/b": "?panew=0.3" };
    const out = stripParam(map, "panew");
    expect(out["/My%20Files"]).toBe("?sort=name%20asc");
    expect(out).not.toHaveProperty("/b");
  });

  test("a map where only some entries carry the param is a new object", () => {
    const map = { "/a": "?sort=name", "/b": "?panew=0.3&sort=size" };
    expect(stripParam(map, "panew")).not.toBe(map);
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
