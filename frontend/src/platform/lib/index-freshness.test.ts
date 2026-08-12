import { beforeEach, expect, test } from "bun:test";
import {
  indexMayAnswer,
  noteFsMutation,
  resetFsMutations,
} from "@platform/lib/index-freshness";

beforeEach(() => resetFsMutations());

test("an untouched folder is still answered by the index", () => {
  expect(indexMayAnswer("/home/me/proj")).toBe(true);
  noteFsMutation("/home/me/other/a.txt");
  expect(indexMayAnswer("/home/me/proj")).toBe(true);
});

test("a mutation makes its own folder walk instead", () => {
  // The corpus for /home/me/proj was built before this file was renamed into
  // it, so the index cannot know the new name and still matches the old one.
  noteFsMutation("/home/me/proj/renamed.txt");
  expect(indexMayAnswer("/home/me/proj")).toBe(false);
});

test("a mutation deep in the subtree invalidates the folder above it", () => {
  // In-folder search is RECURSIVE, so the corpus for /home/me/proj includes
  // everything below it — a change anywhere under it is a change to it.
  noteFsMutation("/home/me/proj/src/deep/x.ts");
  expect(indexMayAnswer("/home/me/proj")).toBe(false);
});

test("renaming an ANCESTOR invalidates the folder below it", () => {
  noteFsMutation("/home/me/proj");
  expect(indexMayAnswer("/home/me/proj/src")).toBe(false);
});

test("a rename several levels down still pins every folder above it to the walk", () => {
  // The invariant the gate exists for, and the reason racing the index against
  // the live walk (listing/source-race) does not weaken it: the index would
  // WIN that race and answer instantly with the pre-rename name. A race fixes a
  // slow answer, never a wrong one — so both directions of the check stay, and
  // "narrow it to the mutated folder itself" is not available.
  noteFsMutation("/home/me/proj/src/deep/nested/renamed.ts");
  for (const folder of [
    "/home/me",
    "/home/me/proj",
    "/home/me/proj/src",
    "/home/me/proj/src/deep",
    "/home/me/proj/src/deep/nested",
  ]) {
    expect(indexMayAnswer(folder)).toBe(false);
  }
});

test("a sibling prefix is not a subtree", () => {
  // /home/me/proj-old must not be read as "inside /home/me/proj".
  noteFsMutation("/home/me/proj-old/a.txt");
  expect(indexMayAnswer("/home/me/proj")).toBe(true);
});

test("trailing slashes do not change the answer", () => {
  noteFsMutation("/home/me/proj/a.txt");
  expect(indexMayAnswer("/home/me/proj/")).toBe(false);
});

test("the record is bounded, keeping the most recent mutations", () => {
  for (let i = 0; i < 500; i++) noteFsMutation(`/home/me/f${i}/x.txt`);
  expect(indexMayAnswer("/home/me/f499")).toBe(false);
  // ...and the set has not grown without limit
  expect(indexMayAnswer("/home/me/f0")).toBe(true);
});
