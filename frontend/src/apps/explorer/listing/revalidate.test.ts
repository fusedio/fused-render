// When an active search adopts a dir-watch generation, and when it sits on the
// one it has. The rule is a UX one — background churn must not disturb results
// the user is reading — so it is decided in one pure place rather than being an
// emergent property of effect ordering.
import { beforeEach, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { shouldReconcile, type RevalidateInput } from "@apps/explorer/listing/revalidate";
import {
  fsMutationCount,
  noteFsMutation,
  resetFsMutations,
  subscribeFsMutations,
} from "@platform/lib/index-freshness";

function input(over: Partial<RevalidateInput> = {}): RevalidateInput {
  return {
    refresh: 5,
    pinned: 4, // a watch bump is pending
    searching: true,
    mutations: 0,
    appliedMutations: 0,
    ...over,
  };
}

test("a watch bump mid-search is NOT adopted", () => {
  // The whole point: no refetch, so no collapse to held rows, so no dimming
  // and no spinner while the user reads.
  expect(shouldReconcile(input())).toBe(false);
});

test("nothing pending is never a reconcile", () => {
  expect(shouldReconcile(input({ refresh: 4, pinned: 4 }))).toBe(false);
  expect(shouldReconcile(input({ refresh: 4, pinned: 4, searching: false }))).toBe(false);
});

test("outside search every bump is adopted, exactly as before", () => {
  expect(shouldReconcile(input({ searching: false }))).toBe(true);
});

test("clearing the search adopts the generation deferred during it", () => {
  // Leaving search flips `searching` false with the bump still pending; that
  // transition alone is the reconcile.
  const pending = input({ searching: true });
  expect(shouldReconcile(pending)).toBe(false);
  expect(shouldReconcile({ ...pending, searching: false })).toBe(true);
});

test("an in-app mutation overrides the deferral", () => {
  // The user renamed something. "Don't bother updating stale results" was
  // never meant to cover the user's own edit.
  expect(shouldReconcile(input({ mutations: 1, appliedMutations: 0 }))).toBe(true);
});

test("an in-app mutation already accounted for does not re-trigger", () => {
  expect(shouldReconcile(input({ mutations: 3, appliedMutations: 3 }))).toBe(false);
});

test("a mutation recorded BEFORE the bump still forces the adoption", () => {
  // Ordering is not guaranteed: the watch event for the user's own rename
  // arrives after the mutation is noted. Nothing pending yet...
  expect(shouldReconcile(input({ refresh: 4, pinned: 4, mutations: 1 }))).toBe(false);
  // ...and when the bump lands it is taken immediately rather than deferred.
  expect(shouldReconcile(input({ refresh: 5, pinned: 4, mutations: 1 }))).toBe(true);
});

test("an index generation swap mid-search is deferred like any other churn", () => {
  // Observed live: a startup scan finished, the index swapped generations, and
  // the results were replaced mid-read — the match total moved, the top hit
  // changed, and the walk placeholder flashed. A completed scan reaches this
  // hook only as watch churn (the worker writes under the home dir), so it has
  // to defer exactly like a touch/rm does, however many generations it jumped.
  for (const refresh of [5, 12, 400]) {
    expect(shouldReconcile(input({ refresh, pinned: 4 }))).toBe(false);
  }
});

test("no amount of accumulated churn ever reconciles itself", () => {
  // There is no threshold at which deferral gives up: only a boundary or an
  // in-app mutation adopts a generation. A scan that bumps the watch a hundred
  // times must leave the results exactly as still as one bump does.
  let pinned = 1;
  for (let refresh = 2; refresh < 100; refresh++) {
    expect(shouldReconcile(input({ refresh, pinned }))).toBe(false);
  }
  expect(pinned).toBe(1);
});

test("focus is not a boundary — the hook must not reconcile on it", () => {
  // Source guard, because this cannot be seen from the pure rule: focus is
  // ambient (the pane focus guard, a split remount at a width threshold, and
  // WebKit restoring focus after a repaint all fire it), so prefetchWalk
  // adopting the pending generation swapped results out from under the reader.
  // It must also request `pinned`, never `refresh`, or it smuggles a newer
  // generation past the deferral.
  const hook = readFileSync(join(import.meta.dir, "useWalkSearch.ts"), "utf8");
  const body = hook.slice(
    hook.indexOf("const prefetchWalk = () =>"),
    hook.indexOf("// Debounced URL mirror"),
  );
  expect(body).not.toContain("reconcile()");
  expect(body).not.toContain("setWalkReq(refresh)");
  expect(body).toContain("setWalkReq(pinned)");
});

// -- the mutation signal itself ------------------------------------------------

beforeEach(() => resetFsMutations());

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
