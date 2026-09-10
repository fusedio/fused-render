import { expect, test } from "bun:test";
import { showSearchExamples } from "@apps/explorer/listing/search-examples";

test("shown only while the field is active and the query is pristine", () => {
  expect(showSearchExamples(true, true)).toBe(true);
});

test("not shown while unfocused, even pristine", () => {
  expect(showSearchExamples(false, true)).toBe(false);
});

test("not shown once anything is typed (not pristine)", () => {
  expect(showSearchExamples(true, false)).toBe(false);
});
