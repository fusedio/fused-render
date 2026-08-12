// The rules a file drag obeys, with no DOM in sight: which gesture a press
// begins, what it picks up, which drops are allowed, and what the ghost says.
//
// These are here rather than in an interaction test because a headless test
// cannot see layout at all — it can see exactly this arithmetic, and every
// wrong drop the wiring could make is a wrong answer from one of these.
import { describe, expect, test } from "bun:test";
import {
  clearFsDrag,
  dragGhostLabel,
  dragPathsFor,
  dropIsValid,
  fsDragInFlight,
  pressStartsDrag,
  refusalNeedsToast,
  springDisarms,
  startFsDrag,
} from "./drag-drop";

const file = (path: string) => ({ path, parentDir: path.slice(0, path.lastIndexOf("/")) });

// Where a drag may start. The whole rule, and it has ONE input — and the input
// is a SNAPSHOT: the selection as it stood BEFORE the press, never as it stands
// once the press has had its effect.
describe("pressStartsDrag", () => {
  test("a press on an already-selected row starts a move-drag", () => {
    expect(pressStartsDrag({ rowWasSelected: true })).toBe(true);
  });

  test("a press on an unselected row starts no drag, wherever it lands", () => {
    // Including the name and icon, which used to be a permanent drag handle.
    // That handle is why a drag started across rows grabbed one file and moved
    // it instead of selecting the rows it crossed: the same pixels cannot serve
    // a move-drag and a sweep, and drag-to-select is the commoner gesture by
    // far. The cost is that moving a single unselected file is two gestures now
    // — click it, then drag it.
    expect(pressStartsDrag({ rowWasSelected: false })).toBe(false);
  });

  test("the rule is exactly the sweep rule inverted", () => {
    // useMarquee calls this same function to find where a SWEEP may start, so
    // the two gestures cannot both claim a pixel and cannot drift apart. If
    // this ever needs a second input, that property is what to preserve.
    for (const rowWasSelected of [true, false]) {
      const drags = pressStartsDrag({ rowWasSelected });
      expect(drags).toBe(rowWasSelected);
      expect(!drags).toBe(!rowWasSelected);
    }
  });
});

// THE SNAPSHOT ARBITER, which is the part that has now cost three rounds.
//
// A press on an unselected row SELECTS it. So there are two readings of "is
// this row selected?" available at any moment after the press — the one from
// before it, and the one the press itself created — and they disagree for
// exactly the case the bug lived in. These tests pin which one the rule is fed;
// the caller (useMarquee, in the capture phase of pointerdown) is what makes
// the value a snapshot, and this is what says why it must be.
describe("the snapshot is what decides, not the live selection", () => {
  // The gesture as the arbiter sees it: the row pressed, and the selection as
  // it stood before the press.
  const gesture = (path: string, selectionBefore: string[]) =>
    pressStartsDrag({ rowWasSelected: selectionBefore.includes(path) });

  test("pressing an UNSELECTED row sweeps, even though the press selects it", () => {
    // The bug, stated as a test. Live, the row is selected a moment after the
    // press and every reading from then on says "move-drag" — which is what a
    // `draggable` attribute is, evaluated when the movement begins rather than
    // when the button went down. From the snapshot the answer is SWEEP, and it
    // stays SWEEP however long the gesture runs.
    const before: string[] = [];
    expect(gesture("/w/notes.md", before)).toBe(false);
    const afterThePress = ["/w/notes.md"];
    expect(pressStartsDrag({ rowWasSelected: afterThePress.includes("/w/notes.md") })).toBe(true);
  });

  test("pressing a row that WAS selected moves it", () => {
    // Select-then-drag: the second press on the same row is the one that moves
    // it, and this is the only way a move-drag ever begins.
    expect(gesture("/w/notes.md", ["/w/notes.md"])).toBe(true);
  });

  test("a press inside a multi-selection moves the whole thing", () => {
    // The press that begins a multi-row drag lands on one of the rows being
    // dragged, and selection defers its collapse to the release for exactly
    // this reason (selection's rowPressAction).
    const before = ["/w/a.md", "/w/b.md", "/w/c.md"];
    expect(gesture("/w/b.md", before)).toBe(true);
    expect(dragPathsFor("/w/b.md", before)).toEqual(before);
  });

  test("pressing OUTSIDE a multi-selection sweeps and does not carry it off", () => {
    // The other half of the same press: an unselected row is not part of what
    // is selected, so the gesture is a sweep and the old selection is replaced
    // rather than moved.
    const before = ["/w/a.md", "/w/b.md"];
    expect(gesture("/w/z.md", before)).toBe(false);
  });

  test("the background is never a drag, whatever is selected", () => {
    // No row pressed at all: `rowWasSelected` is false by construction, so the
    // background always sweeps — including with the whole folder selected.
    expect(pressStartsDrag({ rowWasSelected: false })).toBe(false);
  });
});

// Spring-loading is armed when the drag ENTERS a crumb and cancelled when it
// LEAVES one, and those arrive in an order that makes the naive version cancel
// itself. (They used to be the DOM's dragenter/dragleave; the pointer drag that
// replaced them emits the same pair in the same order — row-drag.ts.)
describe("springDisarms", () => {
  test("leaving the armed crumb cancels it", () => {
    expect(springDisarms("/w", "/w")).toBe(true);
  });

  test("leaving a DIFFERENT crumb does not", () => {
    // The whole bug: dragging from /w to /w/docs fires enter(/w/docs) BEFORE
    // leave(/w), so a leave handler that cancels unconditionally kills the
    // timer that the enter just armed — and the feature never fires unless the
    // pointer reaches a crumb without crossing another one first.
    expect(springDisarms("/w", "/w/docs")).toBe(false);
  });

  test("nothing armed, nothing to cancel", () => {
    expect(springDisarms("/w", null)).toBe(false);
  });
});

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

// A BREADCRUMB CRUMB, which is a drop target as well as a spring-load now
// (Breadcrumb.tsx). Nothing about the rule changes for it — one DropTarget shape
// for every target is the point — but the SHAPE a crumb hands in is unlike a
// row's, so the answers are worth pinning:
//
//   • isDir is always true, asserted by the crumb rather than probed: a path
//     segment you are standing inside cannot be a file, so "not-a-folder" (and
//     with it the one refusal that owes the user a toast) is unreachable here;
//   • a crumb is an ANCESTOR of the listed folder and a dragged row is inside
//     it, so "self" and "descendant" are unreachable too — the drop that a
//     crumb makes possible is the one a row cannot offer, moving entries UP;
//   • "already-there" is reachable, and it is the one that makes the new
//     per-crumb highlight informative instead of a strip that lights up
//     everywhere: a crumb IS the current folder when the listing is at the root
//     of its tree (the root crumb then carries `.last`, and it is the only crumb
//     that is both current and interactive — the tail crumb of a deeper path is
//     a static span with no target at all).
describe("dropIsValid over a crumb", () => {
  const crumb = (path: string) => ({ path, isDir: true });

  test("dropping onto an ancestor crumb moves the entries up", () => {
    // The whole gesture: files in /a/b/c, released on the /a crumb.
    expect(dropIsValid([file("/a/b/c/x.md")], crumb("/a"))).toEqual({ ok: true, dir: "/a" });
    expect(dropIsValid([file("/a/b/c/x.md")], crumb("/a/b"))).toEqual({ ok: true, dir: "/a/b" });
  });

  test("a folder moves up by its crumb too, and takes its own contents", () => {
    // pruneDescendantPaths (fs-move) drops the descendants; the verdict here is
    // only about the folder, and moving /a/b/c/sub to /a is an ordinary move.
    expect(dropIsValid([file("/a/b/c/sub")], crumb("/a"))).toEqual({ ok: true, dir: "/a" });
  });

  test("after a spring-load, the CURRENT-folder crumb is a real move", () => {
    // The post-spring-load geometry, which is where the gesture spends most of
    // its life: the pointer held the /a crumb, the listing followed it to /a, and
    // that crumb is now the current-folder one — while the dragged rows still
    // come from the deeper folder they were picked up in. So the same crumb that
    // would refuse a same-folder drop accepts this one.
    //
    // THIS COVERS THE VERDICT ONLY. That the release actually reaches this
    // target after the strip has re-rendered under a stationary pointer is
    // pointer geometry and mid-drag DOM replacement; no headless test can see it
    // (see refreshDropTarget) and none here claims to.
    expect(dropIsValid([file("/a/b/c/x.md")], crumb("/a"))).toEqual({ ok: true, dir: "/a" });
  });

  test("the crumb for the folder you are IN refuses — nothing would move", () => {
    // The root crumb while the listing IS the root: every dragged row's parent
    // is that folder already.
    expect(dropIsValid([file("/x.md"), file("/y.md")], crumb("/"))).toEqual({
      ok: false,
      reason: "already-there",
    });
  });

  test("the root crumb takes a drop like any other ancestor", () => {
    expect(dropIsValid([file("/a/b/c/x.md")], crumb("/"))).toEqual({ ok: true, dir: "/" });
  });

  test("a crumb's refusal is silent — it wore the reject highlight all hover", () => {
    // Every reachable crumb refusal was painted before the release, and a crumb
    // declares its kind ("1"), so no release on one can need words.
    expect(refusalNeedsToast("already-there", true)).toBe(false);
    expect(refusalNeedsToast("empty", true)).toBe(false);
  });
});

// A refusal the target ALREADY declared needs no words: the row that says
// data-fs-drop-dir="0" wore the no-drop cursor and the reject highlight for the
// whole hover, so a release on it is a gesture the user already saw refused.
// The toast is for the one refusal nobody could see coming — a target whose
// kind was unknown while the pointer was over it (a sidebar bookmark, probed
// optimistically as a folder) turning out to be a file at the release.
describe("refusalNeedsToast", () => {
  test("a target that declared itself a non-folder ends quietly", () => {
    expect(refusalNeedsToast("not-a-folder", true)).toBe(false);
  });

  test("an undeclared target that turns out to be a file has to say so", () => {
    expect(refusalNeedsToast("not-a-folder", false)).toBe(true);
  });

  test("every other refusal is silent, declared or not", () => {
    // self / descendant / already-there / empty all painted the reject
    // highlight from the same dropIsValid the release re-asks, so the user saw
    // them refused before letting go.
    for (const reason of ["self", "descendant", "already-there", "empty"] as const) {
      expect(refusalNeedsToast(reason, false)).toBe(false);
      expect(refusalNeedsToast(reason, true)).toBe(false);
    }
  });
});

describe("the ghost's label", () => {
  test("one entry is named", () => {
    expect(dragGhostLabel(["notes.md"])).toBe("notes.md");
  });

  test("several are counted", () => {
    // Naming one of five would show exactly one of the things being moved and
    // give no hint that the other four are coming — which is what the browser's
    // own drag image did (a snapshot of the one <tr> the press landed on).
    expect(dragGhostLabel(["a.md", "b.md", "c.md"])).toBe("3 items");
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
