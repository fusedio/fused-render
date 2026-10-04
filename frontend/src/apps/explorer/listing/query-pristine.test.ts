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

  // A file host seeds the box with its own full path (SearchField.tsx's
  // `crumbsPath`), not the search scope (`fsPath`, the parent folder) — a
  // committed query still searches the parent, so the scope stays `fsPath`,
  // but the box's own pristine seed is the file's path and must read as
  // untouched too, given as the optional 4th `crumbsFsPath` argument.
  test("the file path a file host actually pre-fills is pristine when passed as crumbsFsPath", () => {
    const file = "/Users/iamsdas/work/index.html";
    expect(isPristineQuery(file, FS_PATH, undefined, file)).toBe(true);
    expect(isPristineQuery(file + "/", FS_PATH, undefined, file)).toBe(true);
    expect(isPristineQuery("~/work/index.html", FS_PATH, HOME, file)).toBe(true);
  });

  test("crumbsFsPath does not loosen what counts as edited when it differs from the query", () => {
    const file = "/Users/iamsdas/work/index.html";
    expect(isPristineQuery("report", FS_PATH, HOME, file)).toBe(false);
    expect(isPristineQuery(FS_PATH + "/x", FS_PATH, undefined, file)).toBe(false);
  });
});
