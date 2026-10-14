// tableToCsv's DOM reader is a one-liner; the quoting rules live in the pure rowsToCsv (bun tests have no DOM).
import { describe, expect, test } from "bun:test";

import { rowsToCsv } from "./threadDom";

describe("rowsToCsv", () => {
  test("a header and two rows", () => {
    expect(rowsToCsv([["a", "b"], ["1", "2"], ["3", "4"]])).toBe("a,b\n1,2\n3,4");
  });
  test("a cell with a comma is quoted", () => {
    expect(rowsToCsv([["x, y", "z"]])).toBe('"x, y",z');
  });
  test("a quote is doubled inside quotes; a newline is quoted", () => {
    expect(rowsToCsv([['say "hi"', "a\nb"]])).toBe('"say ""hi""","a\nb"');
  });
});
