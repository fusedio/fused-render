// The one rule the Apps hub's category chips add on top of "alphabetical":
// a newcomer must meet the learning categories (starters, tutorials, …)
// before the topical ones. Everything below is about that boundary holding —
// including for authored spellings that differ only in case or separators.
import { describe, expect, it } from "bun:test";
import { learnRank, orderCategories } from "./app-categories";

describe("orderCategories", () => {
  it("puts learn categories first, in the authored priority order", () => {
    expect(orderCategories(["guides", "tutorials", "starters"])).toEqual([
      "starters",
      "tutorials",
      "guides",
    ]);
  });

  it("sorts the non-priority tail alphabetically after every learn one", () => {
    expect(orderCategories(["productivity", "geospatial", "local-ai", "starters"])).toEqual([
      "starters",
      "geospatial",
      "local-ai",
      "productivity",
    ]);
  });

  it("ranks a category the same however its name is cased or separated", () => {
    expect(orderCategories(["zebra", "How It Works", "how_to"])).toEqual([
      "How It Works",
      "how_to",
      "zebra",
    ]);
    expect(learnRank("how-it-works")).toBe(learnRank("How_It Works"));
  });

  it("dedups repeats and survives an empty list", () => {
    expect(orderCategories([])).toEqual([]);
    expect(orderCategories(["starters", "starters", "geospatial"])).toEqual([
      "starters",
      "geospatial",
    ]);
  });

  it("never lets an unknown category outrank a learn one", () => {
    // "aaa" wins on plain alphabetical order; the learn rank must beat it.
    expect(orderCategories(["aaa", "tutorials"])).toEqual(["tutorials", "aaa"]);
    expect(learnRank("aaa")).toBeGreaterThan(learnRank("examples"));
  });

  it("leaves the input array untouched", () => {
    const input = ["zebra", "starters"];
    orderCategories(input);
    expect(input).toEqual(["zebra", "starters"]);
  });
});
