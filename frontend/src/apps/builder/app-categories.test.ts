// The one rule the Apps hub's category chips add on top of "alphabetical": the
// curated categories (starters, local-ai, productivity, geospatial) run in
// their authored order, ahead of anything else the workspace turns up.
// Everything below is about that boundary holding — including for authored
// spellings that differ only in case or separators.
import { describe, expect, it } from "bun:test";
import { chipRank, orderCategories, repoChips } from "./app-categories";

describe("orderCategories", () => {
  it("runs the curated categories in their authored order, not alphabetically", () => {
    // Both ends of this row are ones alphabetical order gets wrong: geospatial
    // would lead it and productivity would sit mid-row.
    expect(
      orderCategories(["productivity", "geospatial", "local-ai", "starters"]),
    ).toEqual(["starters", "local-ai", "productivity", "geospatial"]);
  });

  it("sorts the uncurated tail alphabetically after every curated one", () => {
    expect(orderCategories(["zebra", "geospatial", "apple", "starters"])).toEqual([
      "starters",
      "geospatial",
      "apple",
      "zebra",
    ]);
  });

  it("ranks a curated category the same however its name is cased or separated", () => {
    // "aaa" wins on locale order, so each spelling below only leads the row if
    // normalize actually matched it against CHIP_ORDER's "local-ai" — drop the
    // case-folding or the separator stripping and the ordering breaks, not just
    // the rank-equality check.
    expect(orderCategories(["aaa", "Local AI"])).toEqual(["Local AI", "aaa"]);
    expect(orderCategories(["aaa", "local_ai"])).toEqual(["local_ai", "aaa"]);
    expect(orderCategories(["aaa", "LOCAL-AI"])).toEqual(["LOCAL-AI", "aaa"]);
    expect(chipRank("local-ai")).toBe(chipRank("Local_AI"));
    // CHIP_ORDER spells this entry with a hyphen, so it only ranks at all if
    // the rank map is keyed on the normalized name too.
    expect(chipRank("local-ai")).toBeLessThan(chipRank("aaa"));
  });

  it("dedups repeats and survives an empty list", () => {
    expect(orderCategories([])).toEqual([]);
    expect(orderCategories(["starters", "starters", "geospatial"])).toEqual([
      "starters",
      "geospatial",
    ]);
  });

  it("never lets an uncurated category outrank a curated one", () => {
    // "aaa" wins on plain alphabetical order; the curated rank must beat it.
    expect(orderCategories(["aaa", "geospatial"])).toEqual(["geospatial", "aaa"]);
    expect(chipRank("aaa")).toBeGreaterThan(chipRank("geospatial"));
  });

  it("leaves the input array untouched", () => {
    const input = ["zebra", "starters"];
    orderCategories(input);
    expect(input).toEqual(["zebra", "starters"]);
  });
});

// The Folders facet groups by SOURCE, and an exported `.fused` has none — so its
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
