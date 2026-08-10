// What a drag-to-move actually does to a batch, before any server is involved:
// which entries change folder, which are carried by a folder that is itself on
// the move, and which are already where they were dropped.
//
// fs-move reaches the api layer and from there the router, which reads
// `location` at module scope — so the stub precedes the (therefore dynamic)
// import, the same trade fs-actions.test.ts makes rather than carrying a DOM.
import { expect, test } from "bun:test";

(globalThis as { location?: unknown }).location = new URL("http://x/");
const { movePlan } = await import("@apps/explorer/lib/fs-move");

test("a plain batch moves everything it was given", () => {
  expect(movePlan(["/w/a.md", "/w/b.md"], "/w/docs")).toEqual({
    move: ["/w/a.md", "/w/b.md"],
    skip: [],
  });
});

test("an entry already in the target is skipped, not moved onto itself", () => {
  // A search selection can hold hits from several folders; the ones already in
  // the drop target have nowhere to go, and renaming src to src would 409.
  expect(movePlan(["/w/a.md", "/w/docs/b.md"], "/w/docs")).toEqual({
    move: ["/w/a.md"],
    skip: ["/w/docs/b.md"],
  });
});

test("a dragged folder carries its own contents — descendants leave the batch", () => {
  // Moving "/w/src" takes "/w/src/lib.ts" with it; moving the child separately
  // afterwards would 404 on a path its parent already took, and that failure
  // would abort the rest of the batch.
  expect(movePlan(["/w/src", "/w/src/lib.ts", "/w/n.md"], "/w/docs")).toEqual({
    move: ["/w/src", "/w/n.md"],
    skip: [],
  });
});

test("the target's trailing slash doesn't turn a no-op into a move", () => {
  expect(movePlan(["/w/docs/b.md"], "/w/docs/").skip).toEqual(["/w/docs/b.md"]);
});

test("the filesystem root is a target like any other", () => {
  // normDir/join keep the root from producing "//name" — the skip test has to
  // see "/a.md" as already at the root.
  expect(movePlan(["/a.md", "/w/b.md"], "/")).toEqual({
    move: ["/w/b.md"],
    skip: ["/a.md"],
  });
});

test("an empty drop plans nothing", () => {
  expect(movePlan([], "/w/docs")).toEqual({ move: [], skip: [] });
});
