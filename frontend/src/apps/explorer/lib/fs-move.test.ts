// What a drag-to-move actually does to a batch, before any server is involved:
// which entries change folder, which are carried by a folder that is itself on
// the move, and which are already where they were dropped — plus what the move
// REPORTS, which is where undo gets its inverse from.
//
// fs-move reaches the api layer and from there the router, which reads
// `location` at module scope — so the stub precedes the (therefore dynamic)
// import, the same trade fs-actions.test.ts makes rather than carrying a DOM.
import { expect, test } from "bun:test";

(globalThis as { location?: unknown }).location = new URL("http://x/");
// A failed move toasts, and the toast store schedules its own dismissal off
// `window` — the one DOM thing this file needs, so it gets the one method.
(globalThis as { window?: unknown }).window = { setTimeout: () => 0, clearTimeout: () => {} };

// A filesystem for the report tests below: `takenIn` decides which names each
// target folder already holds (so freePastePath's dedupe can be driven), and
// `failRename` makes one source refuse to move.
const takenIn = new Map<string, string[]>();
let failRename: string | null = null;

(globalThis as { fetch?: unknown }).fetch = ((url: string, init?: { body?: string }) => {
  const body = init?.body ? JSON.parse(init.body) : {};
  const json = (data: unknown, status = 200) =>
    Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(data) });
  if (url.startsWith("/api/fs/stat")) return json({ is_dir: false });
  if (url.startsWith("/api/fs/list")) {
    const dir = decodeURIComponent(new URL("http://x" + url).searchParams.get("path") || "");
    return json({ entries: (takenIn.get(dir) || []).map((name) => ({ name })) });
  }
  if (url === "/api/fs/rename") {
    if (body.src === failRename) return json({ error: "readonly" }, 403);
    return json({ path: body.dst });
  }
  return json({});
}) as unknown as typeof fetch;

const { movePlan, moveEntriesInto } = await import("@apps/explorer/lib/fs-move");

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

// --- what the move REPORTS ---------------------------------------------------
//
// `pairs` is the undo entry for the move (lib/fs-undo): each entry's source and
// the destination it actually landed at. Both halves of "actually" are tested
// here, because both are ways an inverse built from the INTENDED batch would
// rename the wrong path.

test("the report pairs each source with the destination that really landed", async () => {
  // "report.csv" is taken in the target, so freePastePath keeps both and the
  // entry lands as "report copy.csv". An undo built from the asked-for name
  // would try to move a path that does not exist.
  takenIn.set("/w/docs", ["report.csv"]);
  const report = await moveEntriesInto(["/w/report.csv", "/w/notes.md"], "/w/docs");
  expect(report.failed).toBeNull();
  expect(report.pairs).toEqual([
    { from: "/w/report.csv", to: "/w/docs/report copy.csv" },
    { from: "/w/notes.md", to: "/w/docs/notes.md" },
  ]);
  // `moved` is unchanged — it is what callers re-anchor the selection on.
  expect(report.moved).toEqual(["/w/docs/report copy.csv", "/w/docs/notes.md"]);
  takenIn.clear();
});

test("only the entries that LANDED are paired", async () => {
  // The loop stops at its first failure, so the batch is half-moved; an undo
  // entry covering the whole intended batch would try to un-move the two
  // entries that never went anywhere.
  failRename = "/w/b.md";
  const report = await moveEntriesInto(["/w/a.md", "/w/b.md", "/w/c.md"], "/w/docs");
  expect(report.failed?.path).toBe("/w/b.md");
  expect(report.pairs).toEqual([{ from: "/w/a.md", to: "/w/docs/a.md" }]);
  failRename = null;
});

test("a move with nothing to move reports no pairs", async () => {
  // Dropping entries into the folder they already live in: nothing renamed, so
  // there is nothing to undo and no entry may be recorded.
  const report = await moveEntriesInto(["/w/docs/a.md"], "/w/docs");
  expect(report.pairs).toEqual([]);
  expect(report.moved).toEqual([]);
});
