import { expect, test } from "bun:test";
import { showSearchExamples } from "@apps/explorer/listing/search-examples";

// ITEM 8 (running-screen review, 2026-09-10) deleted this file's own
// `buildSearchExamples` (and the `SearchExample`/`ExampleEntry` types) along
// with the tests that only covered ITS derivation (most common extension
// wins, alphabetical tie-break, hidden/directory entries don't vote, the
// escape row omitted at home) — that behaviour no longer exists; the panel
// SearchField.tsx renders now is three sentences of fixed prose, not a
// folder-derived example set. `showSearchExamples` itself — WHEN the panel
// appears, as opposed to what it contains — is unchanged, and its coverage
// below survives untouched.
test("shown only while the field is active and the query is pristine", () => {
  expect(showSearchExamples(true, true)).toBe(true);
});

test("not shown while unfocused, even pristine", () => {
  expect(showSearchExamples(false, true)).toBe(false);
});

test("not shown once anything is typed (not pristine)", () => {
  expect(showSearchExamples(true, false)).toBe(false);
});
