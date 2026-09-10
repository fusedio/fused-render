import { expect, test } from "bun:test";
import { buildSearchExamples, showSearchExamples } from "@apps/explorer/listing/search-examples";

test("shown only while the field is active and the query is pristine", () => {
  expect(showSearchExamples(true, true)).toBe(true);
});

test("not shown while unfocused, even pristine", () => {
  expect(showSearchExamples(false, true)).toBe(false);
});

test("not shown once anything is typed (not pristine)", () => {
  expect(showSearchExamples(true, false)).toBe(false);
});

// SPEC-omnibox-search-affordance.md correction (2026-09-10): the examples
// must come from the folder's own entries, not a literal that returns zero
// rows anywhere but the author's own machine.
const FS_PATH = "/Users/iamsdas/Downloads";
const HOME = "/Users/iamsdas";

function entry(name: string, is_dir = false) {
  return { name, is_dir };
}

test("the most common extension wins", () => {
  const entries = [entry("a.zip"), entry("b.zip"), entry("c.pdf")];
  const examples = buildSearchExamples(entries, FS_PATH, HOME);
  expect(examples[0]).toEqual({ pattern: "*.zip", hint: "ZIP files in this folder" });
});

test("a tie breaks alphabetically, deterministically", () => {
  const entries = [entry("a.zip"), entry("b.pdf")];
  const examples = buildSearchExamples(entries, FS_PATH, HOME);
  expect(examples[0].pattern).toBe("*.pdf");
});

test("slots 1 and 2 always carry the same extension", () => {
  const entries = [entry("a.json"), entry("b.json"), entry("c.xlsx")];
  const examples = buildSearchExamples(entries, FS_PATH, HOME);
  expect(examples[0]).toEqual({ pattern: "*.json", hint: "JSON files in this folder" });
  expect(examples[1]).toEqual({
    pattern: ".json",
    hint: "JSON files in this folder and everything below it",
  });
});

test("the escape example reuses the same extension and points at ~", () => {
  const entries = [entry("a.json")];
  const examples = buildSearchExamples(entries, FS_PATH, HOME);
  expect(examples[2]).toEqual({
    pattern: "~/*/*.json",
    hint: "searches from ~ instead of here",
  });
});

test("directories don't vote", () => {
  const entries = [entry("node_modules", true), entry("a.pdf")];
  const examples = buildSearchExamples(entries, FS_PATH, HOME);
  expect(examples[0].pattern).toBe("*.pdf");
});

test("hidden entries don't vote", () => {
  const entries = [entry(".env.json"), entry("a.pdf")];
  const examples = buildSearchExamples(entries, FS_PATH, HOME);
  expect(examples[0].pattern).toBe("*.pdf");
});

test("a folder with no extensioned files renders no invented example", () => {
  expect(buildSearchExamples([entry("Makefile"), entry("bin", true)], FS_PATH, HOME)).toEqual([]);
  expect(buildSearchExamples([], FS_PATH, HOME)).toEqual([]);
});

test("the escape row is omitted while standing at home itself", () => {
  const examples = buildSearchExamples([entry("a.pdf")], HOME, HOME);
  expect(examples).toEqual([
    { pattern: "*.pdf", hint: "PDF files in this folder" },
    { pattern: ".pdf", hint: "PDF files in this folder and everything below it" },
  ]);
});

test("the escape row is omitted when standing at home with a trailing slash", () => {
  const examples = buildSearchExamples([entry("a.pdf")], HOME + "/", HOME);
  expect(examples.length).toBe(2);
});

test("extension display is lowercased in the pattern, uppercased in the hint", () => {
  const examples = buildSearchExamples([entry("A.PDF")], FS_PATH, HOME);
  expect(examples[0]).toEqual({ pattern: "*.pdf", hint: "PDF files in this folder" });
});
