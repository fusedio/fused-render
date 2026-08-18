import { beforeEach, expect, test } from "bun:test";
import {
  RESCAN_PENDING_MAX_MS,
  fsMutationCount,
  indexLifecycleCount,
  indexRescanPending,
  noteFsMutation,
  noteIndexLifecycle,
  resetFsMutations,
  subscribeFsMutations,
} from "@platform/lib/index-freshness";

beforeEach(() => resetFsMutations());

// -- the mutation signal itself ------------------------------------------------

test("noting a mutation increments the count and notifies subscribers", () => {
  const seen: number[] = [];
  const off = subscribeFsMutations(() => seen.push(fsMutationCount()));
  expect(fsMutationCount()).toBe(0);
  noteFsMutation("/home/me/proj/a.txt");
  noteFsMutation("/home/me/proj/b.txt");
  expect(seen).toEqual([1, 2]);
  off();
  noteFsMutation("/home/me/proj/c.txt");
  expect(seen).toEqual([1, 2]); // unsubscribed
  expect(fsMutationCount()).toBe(3); // ...but the count keeps moving
});

test("the count is what distinguishes an in-app change from watch churn", () => {
  // Watch churn does not touch this module at all, which is precisely why it
  // can be used to tell the two apart.
  expect(fsMutationCount()).toBe(0);
  noteFsMutation("/home/me/proj/x.txt");
  expect(fsMutationCount()).toBe(1);
});

// -- "the index is being put right" --------------------------------------------
//
// This replaces the old `indexMayAnswer` gate. That one answered "may the
// index answer for this folder?" with a NO that lasted the whole session, and
// the in-folder search routed around it by walking the folder live. The server
// now rescans the folder the app changed (server/index_touch.py), so the
// question is no longer whether to trust the index but whether the fix is
// still on its way — which is a claim about time, not about a folder.

test("a mutation leaves a rescan pending", () => {
  expect(indexRescanPending()).toBe(false);
  noteFsMutation("/home/me/proj/renamed.txt");
  expect(indexRescanPending()).toBe(true);
});

test("a completed scan is what clears it", () => {
  // The scan the mutation triggered has landed: the index now spells the new
  // name, and there is nothing left to caption.
  noteFsMutation("/home/me/proj/renamed.txt");
  noteIndexLifecycle();
  expect(indexRescanPending()).toBe(false);
});

test("a mutation after a scan is pending again", () => {
  noteFsMutation("/home/me/proj/a.txt");
  noteIndexLifecycle();
  noteFsMutation("/home/me/proj/b.txt");
  expect(indexRescanPending()).toBe(true);
});

test("a rescan nobody ever runs stops being claimed", () => {
  // The server refuses a rescan it must not run — a mount, "/", an ignored
  // tree, another filesystem — and it does not report that back: the client
  // asks for nothing, it only says what it did. So the claim has to expire on
  // its own, or mutating a file on a mounted bucket shows "indexing…" for the
  // rest of the session AND suppresses the `behind` caveat that is the true
  // one there.
  noteFsMutation("/home/me/proj/a.txt", 0);
  expect(indexRescanPending(0)).toBe(true);
  expect(indexRescanPending(RESCAN_PENDING_MAX_MS - 1)).toBe(true);
  expect(indexRescanPending(RESCAN_PENDING_MAX_MS + 1)).toBe(false);
});

test("a later mutation renews the claim", () => {
  noteFsMutation("/home/me/proj/a.txt", 0);
  noteFsMutation("/home/me/proj/b.txt", RESCAN_PENDING_MAX_MS - 1);
  expect(indexRescanPending(RESCAN_PENDING_MAX_MS + 1)).toBe(true);
});

test("a completed scan still clears it outright, whatever the clock says", () => {
  noteFsMutation("/home/me/proj/a.txt", 0);
  noteIndexLifecycle();
  expect(indexRescanPending(0)).toBe(false);
});

test("the lifecycle count still moves for the fetch keys that ride it", () => {
  expect(indexLifecycleCount()).toBe(0);
  noteIndexLifecycle();
  expect(indexLifecycleCount()).toBe(1);
});
