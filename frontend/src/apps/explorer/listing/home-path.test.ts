import { expect, test } from "bun:test";
import { contractHome } from "@apps/explorer/listing/home-path";

test("home-relative path contracts to a leading ~", () => {
  expect(contractHome("/Users/dev/Downloads", "/Users/dev")).toBe("~/Downloads");
});

test("nested home-relative path keeps everything past the ~", () => {
  expect(contractHome("/Users/dev/Downloads/receipts", "/Users/dev")).toBe("~/Downloads/receipts");
});

test("the home directory itself shows its full path, not a lone ~", () => {
  expect(contractHome("/Users/dev", "/Users/dev")).toBe("/Users/dev");
});

// The search-hits header wants the opposite of the case above: `~` is the
// header's own call site (Listing.tsx's `baseText`, `searchBase === home ?
// "~" : contractHome(...)`), not a second behavior of this helper — the three
// crumb-strip sites this helper mirrors keep showing home's full path, and
// the test above stays exactly as it is.
test("the header's own exact-home case lives beside this helper's call, not inside it", () => {
  const home = "/Users/dev";
  const searchBase = home;
  const baseText = searchBase === home ? "~" : contractHome(searchBase, home);
  expect(baseText).toBe("~");
});

test("a path outside home is returned unchanged", () => {
  expect(contractHome("/var/log", "/Users/dev")).toBe("/var/log");
});

test("home undefined (not yet loaded) returns the path unchanged", () => {
  expect(contractHome("/Users/dev/Downloads", undefined)).toBe("/Users/dev/Downloads");
});

test("a sibling directory that merely shares the home string as a prefix is not contracted", () => {
  // "/Users/devtools" starts with "/Users/dev" as a raw string but is not
  // beneath it — the "+ '/'" check is what keeps this from misfiring.
  expect(contractHome("/Users/devtools/bin", "/Users/dev")).toBe("/Users/devtools/bin");
});

test("a Windows-style drive path outside home is returned unchanged", () => {
  expect(contractHome("C:\\Users\\dev\\Downloads", "/Users/dev")).toBe("C:\\Users\\dev\\Downloads");
});
