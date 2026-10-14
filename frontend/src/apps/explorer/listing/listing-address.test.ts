import { describe, expect, test } from "bun:test";
import { listingAddress } from "@apps/explorer/listing/listing-address";

const home = "/Users/me";
const fsPath = "/Users/me/proj";

describe("listingAddress", () => {
  test("a bare filter word is not a path candidate", () => {
    expect(listingAddress("readme", fsPath, home)).toBeNull();
  });

  test("a glob is never a candidate, even with a slash", () => {
    expect(listingAddress("a/*.py", fsPath, home)).toBeNull();
    expect(listingAddress("*.py", fsPath, home)).toBeNull();
  });

  test("a relative path resolves against the folder being searched", () => {
    expect(listingAddress("sub/dir", fsPath, home)).toBe("/Users/me/proj/sub/dir");
  });

  test("an absolute path passes through, trailing slash stripped", () => {
    expect(listingAddress("/tmp/x/", fsPath, home)).toBe("/tmp/x");
  });

  test("a tilde path expands against home", () => {
    expect(listingAddress("~/Downloads", fsPath, home)).toBe("/Users/me/Downloads");
    expect(listingAddress("~", fsPath, home)).toBe(home);
  });

  test("a tilde path with no known home is not a candidate", () => {
    expect(listingAddress("~/Downloads", fsPath, undefined)).toBeNull();
  });

  test("root stays root", () => {
    expect(listingAddress("/", fsPath, home)).toBe("/");
  });
});
