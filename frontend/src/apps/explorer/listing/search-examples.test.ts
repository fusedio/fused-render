import { expect, test } from "bun:test";
import { showSearchExamples } from "@apps/explorer/listing/search-examples";

test("shown only while the field is active and the query is empty", () => {
  expect(showSearchExamples(true, "", false)).toBe(true);
});

test("not shown while unfocused, even with an empty query", () => {
  expect(showSearchExamples(false, "", false)).toBe(false);
});

test("not shown once anything is typed", () => {
  expect(showSearchExamples(true, "a", false)).toBe(false);
});

test("the completion dropdown always wins the surface once it has something to show", () => {
  // Query non-empty but showCompletion somehow true is the completion's own
  // gate to police; this function only owns the empty-query side, and a
  // caller that finds showCompletion true must never also ask for examples.
  expect(showSearchExamples(true, "", true)).toBe(false);
});
