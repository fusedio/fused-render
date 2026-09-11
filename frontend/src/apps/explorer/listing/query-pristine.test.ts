import { describe, expect, test } from "bun:test";
import { isPristineQuery } from "@apps/explorer/listing/query-pristine";

const FS_PATH = "/Users/iamsdas";
const HOME = "/Users/iamsdas/../iamsdas".replace("/../iamsdas", ""); // "/Users/iamsdas" — same as FS_PATH, spelled independently

describe("isPristineQuery", () => {
  test("empty is pristine", () => {
    expect(isPristineQuery("", FS_PATH, HOME)).toBe(true);
    expect(isPristineQuery("   ", FS_PATH, HOME)).toBe(true);
  });

  test("the exact pre-filled absolute path is pristine", () => {
    expect(isPristineQuery("/Users/iamsdas", FS_PATH, undefined)).toBe(true);
  });

  test("a trailing slash on either side is tolerated", () => {
    expect(isPristineQuery("/Users/iamsdas/", FS_PATH, undefined)).toBe(true);
    expect(isPristineQuery("/Users/iamsdas", "/Users/iamsdas/", undefined)).toBe(true);
  });

  test("the ~-contracted form the box would actually pre-fill under home is also pristine", () => {
    // `contractHome` only contracts a STRICT descendant of home (its own
    // rule — home itself renders its full path, not a lone "~"), so the
    // tilde form is exercised on a folder actually under home, not home
    // itself.
    expect(isPristineQuery("~/work", "/Users/iamsdas/work", HOME)).toBe(true);
    expect(isPristineQuery("~/work/", "/Users/iamsdas/work", HOME)).toBe(true);
  });

  test("a same-prefix but different folder is edited, not pristine — segment equality, not a string prefix", () => {
    expect(isPristineQuery("/Users/iamsdas2", FS_PATH, undefined)).toBe(false);
  });

  test("one extra character anywhere is edited", () => {
    expect(isPristineQuery("/Users/iamsdas/x", FS_PATH, undefined)).toBe(false);
    expect(isPristineQuery("/Users/iamsda", FS_PATH, undefined)).toBe(false);
  });

  test("a real search query is edited", () => {
    expect(isPristineQuery("report", FS_PATH, HOME)).toBe(false);
    expect(isPristineQuery("/Users/iamsdas/*.csv", FS_PATH, HOME)).toBe(false);
  });
});
