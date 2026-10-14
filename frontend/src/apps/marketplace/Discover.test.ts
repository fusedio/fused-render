// Shelves show whole rows of the grid as it is actually laid out (2, 3 or 4
// columns), never a lone orphan card on a second row.
import { describe, expect, test } from "bun:test";
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();
const { shelfCount } = await import("./Discover");

describe("shelfCount", () => {
  test("fewer apps than one row are all shown", () => {
    expect(shelfCount(3, 4)).toBe(3);
    expect(shelfCount(2, 2)).toBe(2);
  });
  test("drops the orphan row", () => {
    expect(shelfCount(4, 3)).toBe(3);
    expect(shelfCount(3, 2)).toBe(2);
    expect(shelfCount(5, 4)).toBe(4);
  });
  test("caps at two whole rows", () => {
    expect(shelfCount(12, 3)).toBe(6);
    expect(shelfCount(12, 4)).toBe(8);
    expect(shelfCount(7, 2)).toBe(4);
    expect(shelfCount(9, 1)).toBe(2);
  });
});
