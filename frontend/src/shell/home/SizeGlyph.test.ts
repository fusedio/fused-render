import { describe, expect, test } from "bun:test";
import { footprintCells } from "./SizeGlyph";

const rows = (c: boolean[]) => [c.slice(0, 4), c.slice(4)].map((r) => r.map((x) => (x ? "#" : ".")).join(""));

describe("footprintCells", () => {
  test("4x1 fills the top row", () => expect(rows(footprintCells("4x1"))).toEqual(["####", "...."]));
  test("2x1", () => expect(rows(footprintCells("2x1"))).toEqual(["##..", "...."]));
  test("1x2 fills the first column", () => expect(rows(footprintCells("1x2"))).toEqual(["#...", "#..."]));
  test("2x2", () => expect(rows(footprintCells("2x2"))).toEqual(["##..", "##.."]));
  test("1x1", () => expect(rows(footprintCells("1x1"))).toEqual(["#...", "...."]));
});
