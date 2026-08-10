// The rules a file drag obeys, with no DOM in sight: what a press on a row
// picks up, which drops are allowed, and how the payload survives the trip
// through the drag's own dataTransfer.
//
// These are here rather than in an interaction test because a headless test
// cannot see layout at all — it can see exactly this arithmetic, and every
// wrong drop the wiring could make is a wrong answer from one of these.
import { describe, expect, test } from "bun:test";
import {
  FS_DRAG_MIME,
  carriesFsDrag,
  clearFsDrag,
  decodeDragPaths,
  dragPathsFor,
  dropIsValid,
  encodeDragPaths,
  fsDragInFlight,
  startFsDrag,
} from "./drag-drop";

const file = (path: string) => ({ path, parentDir: path.slice(0, path.lastIndexOf("/")) });

describe("dragPathsFor", () => {
  test("dragging a row inside the selection drags the whole selection", () => {
    expect(dragPathsFor("/w/b", ["/w/a", "/w/b", "/w/c"])).toEqual(["/w/a", "/w/b", "/w/c"]);
  });

  test("dragging an unselected row drags only that row", () => {
    // Finder/Explorer both drop the old selection here — the press that starts
    // the drag is also a click, and a click selects.
    expect(dragPathsFor("/w/z", ["/w/a", "/w/b"])).toEqual(["/w/z"]);
  });

  test("with nothing selected, the pressed row is the drag", () => {
    expect(dragPathsFor("/w/a", [])).toEqual(["/w/a"]);
  });

  test("the selection's own order is kept, not the pressed row first", () => {
    // The caller passes the RENDERED order, so a batch move processes rows
    // top-to-bottom however they were clicked (same rule as the batch ops).
    expect(dragPathsFor("/w/c", ["/w/a", "/w/b", "/w/c"])[0]).toBe("/w/a");
  });
});

describe("dropIsValid", () => {
  const dragged = [file("/w/notes.md")];

  test("into a sibling folder", () => {
    expect(dropIsValid(dragged, { path: "/w/docs", isDir: true })).toEqual({
      ok: true,
      dir: "/w/docs",
    });
  });

  test("onto a file is not a drop at all", () => {
    expect(dropIsValid(dragged, { path: "/w/other.md", isDir: false })).toEqual({
      ok: false,
      reason: "not-a-folder",
    });
  });

  test("onto itself", () => {
    expect(dropIsValid([file("/w/docs")], { path: "/w/docs", isDir: true })).toEqual({
      ok: false,
      reason: "self",
    });
  });

  test("a folder cannot be dropped inside itself", () => {
    // The move would make the folder its own ancestor; the server would refuse
    // it, but the pointer must say so before the release, not after.
    expect(dropIsValid([file("/w/docs")], { path: "/w/docs/notes", isDir: true })).toEqual({
      ok: false,
      reason: "descendant",
    });
    expect(dropIsValid([file("/w/docs")], { path: "/w/docs/a/b/c", isDir: true })).toEqual({
      ok: false,
      reason: "descendant",
    });
  });

  test("a sibling whose name merely starts the same is not a descendant", () => {
    // "/w/docs2" is not inside "/w/docs" — the separator is what makes it one.
    expect(dropIsValid([file("/w/docs")], { path: "/w/docs2", isDir: true }).ok).toBe(true);
  });

  test("onto the folder the entry is already in is a no-op, not a move", () => {
    expect(dropIsValid(dragged, { path: "/w", isDir: true })).toEqual({
      ok: false,
      reason: "already-there",
    });
  });

  test("a mixed batch still moves the entries that would actually move", () => {
    // Dragged out of a search listing: one hit is already in the target folder
    // and one is not. Refusing the whole drop because of the first would be
    // refusing the move the user asked for.
    const mixed = [file("/w/a.md"), file("/w/deep/b.md")];
    expect(dropIsValid(mixed, { path: "/w", isDir: true })).toEqual({ ok: true, dir: "/w" });
  });

  test("trailing slashes do not invent a move", () => {
    // The listing's own folder path arrives as "/w/" from some call sites and
    // "/w" from others; the no-op check has to see through that or the
    // background target would happily "move" every row onto itself.
    expect(dropIsValid(dragged, { path: "/w/", isDir: true }).ok).toBe(false);
    expect(dropIsValid([file("/w/docs")], { path: "/w/docs/", isDir: true })).toEqual({
      ok: false,
      reason: "self",
    });
  });

  test("the filesystem root is a folder like any other", () => {
    expect(dropIsValid([{ path: "/w", parentDir: "/" }], { path: "/", isDir: true })).toEqual({
      ok: false,
      reason: "already-there",
    });
    expect(dropIsValid([{ path: "/w/a.md", parentDir: "/w" }], { path: "/", isDir: true })).toEqual({
      ok: true,
      dir: "/",
    });
  });

  test("an empty drag drops nowhere", () => {
    expect(dropIsValid([], { path: "/w/docs", isDir: true })).toEqual({
      ok: false,
      reason: "empty",
    });
  });

  test("one bad entry rejects the batch", () => {
    // Dropping three things into a folder that is one of them cannot be split
    // into "the good two" — the target itself is on the move.
    const batch = [file("/w/a.md"), file("/w/docs"), file("/w/b.md")];
    expect(dropIsValid(batch, { path: "/w/docs", isDir: true })).toEqual({
      ok: false,
      reason: "self",
    });
  });
});

describe("the drag payload", () => {
  test("round-trips through dataTransfer's string channel", () => {
    const paths = ["/w/a b.md", "/w/π/c.md"];
    expect(decodeDragPaths(encodeDragPaths(paths))).toEqual(paths);
  });

  test("junk on the wire decodes to nothing, never to a path", () => {
    // dataTransfer carries whatever the source put there — including another
    // app's payload under the same generic types.
    expect(decodeDragPaths(null)).toEqual([]);
    expect(decodeDragPaths("")).toEqual([]);
    expect(decodeDragPaths("not json")).toEqual([]);
    expect(decodeDragPaths('{"paths":"/w/a"}')).toEqual([]);
    expect(decodeDragPaths("[1,2,3]")).toEqual([]);
  });

  test("carriesFsDrag reads the type list, which is all dragover is given", () => {
    // getData() is blacked out during dragover for privacy, so the ONLY thing a
    // drop target can ask mid-drag is whether our MIME is among the types.
    expect(carriesFsDrag([FS_DRAG_MIME, "text/plain"])).toBe(true);
    expect(carriesFsDrag(["text/plain"])).toBe(false);
    expect(carriesFsDrag(["Files"])).toBe(false); // a drag in from the OS: not ours
    expect(carriesFsDrag([])).toBe(false);
  });
});

describe("the in-flight drag store", () => {
  test("holds the dragged entries across a remount", () => {
    // A spring-loaded breadcrumb navigation remounts the Listing mid-drag, so
    // the entries cannot live in its component state — the drop target in the
    // NEW folder still has to know what is coming and whether it may land.
    startFsDrag([file("/w/a.md")]);
    expect(fsDragInFlight()).toEqual([file("/w/a.md")]);
    clearFsDrag();
    expect(fsDragInFlight()).toEqual([]);
  });

  test("no drag in flight rejects every drop", () => {
    clearFsDrag();
    expect(dropIsValid(fsDragInFlight(), { path: "/w/docs", isDir: true })).toEqual({
      ok: false,
      reason: "empty",
    });
  });
});
