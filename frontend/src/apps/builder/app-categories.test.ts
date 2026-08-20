// The one rule the Apps hub's category chips add on top of "alphabetical":
// a newcomer must meet the learning categories (starters, tutorials, …)
// before the topical ones. Everything below is about that boundary holding —
// including for authored spellings that differ only in case or separators.
import { describe, expect, it } from "bun:test";
import { learnRank, orderCategories, repoChips } from "./app-categories";

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

  it("ranks a learn category the same however its name is cased or separated", () => {
    // "aaa" wins on locale order, so each spelling below only leads the row if
    // normalize actually matched it against LEARN_ORDER's "howitworks" — drop
    // the case-folding or the separator stripping and the ordering breaks, not
    // just the rank-equality check.
    expect(orderCategories(["aaa", "How It Works"])).toEqual(["How It Works", "aaa"]);
    expect(orderCategories(["aaa", "how_it_works"])).toEqual(["how_it_works", "aaa"]);
    expect(orderCategories(["aaa", "How-It-Works"])).toEqual(["How-It-Works", "aaa"]);
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

// The Repo facet groups by SOURCE, and an exported `.fused` has none — so its
// "Fused-App" tag must never become a chip, however many such files the index
// turns up.
describe("repoChips", () => {
  const folder = (tag: string) => ({ tag });
  const appfile = (tag: string) => ({ tag, kind: "appfile" });

  it("drops appfile rows and keeps every folder-shaped tag", () => {
    expect(
      repoChips([folder("showcase"), appfile("Fused-App"), folder("linked")]),
    ).toEqual(["linked", "showcase"]);
  });

  it("leaves no chip behind when the hub holds nothing but app files", () => {
    expect(repoChips([appfile("Fused-App"), appfile("Fused-App")])).toEqual([]);
  });

  it("excludes by kind, not by the tag text — rewording cannot restore the chip", () => {
    // A folder that happens to carry the same tag string still gets its chip:
    // the rule is about what the row IS, not what it is called.
    expect(repoChips([appfile("anything"), folder("Fused-App")])).toEqual([
      "Fused-App",
    ]);
  });

  it("dedups repeats and survives an empty list", () => {
    expect(repoChips([])).toEqual([]);
    expect(repoChips([folder("showcase"), folder("showcase")])).toEqual(["showcase"]);
  });

  it("leaves the input array untouched", () => {
    const input = [folder("zebra"), appfile("Fused-App")];
    repoChips(input);
    expect(input).toHaveLength(2);
  });
});
