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
  pressIsSuppressed,
  pressStartsDrag,
  refusalNeedsToast,
  springDisarms,
  startFsDrag,
} from "./drag-drop";

const file = (path: string) => ({ path, parentDir: path.slice(0, path.lastIndexOf("/")) });

// Where a drag may start. The item is its icon+name handle; the rest of the
// row — dead space, size, modified — is marquee ground, same as the
// background. `rowWasSelected` is still a SNAPSHOT: the selection as it stood
// BEFORE the press, never as it stands once the press has had its effect.
describe("pressStartsDrag", () => {
  const press = (p: Partial<{ onHandle: boolean; rowWasSelected: boolean; modified: boolean }>) =>
    pressStartsDrag({ onHandle: false, rowWasSelected: false, modified: false, ...p });

  test("a press on the icon+name handle starts a move-drag, selected or not", () => {
    expect(press({ onHandle: true })).toBe(true);
    expect(press({ onHandle: true, rowWasSelected: true })).toBe(true);
  });

  test("a press on an already-selected row starts a move-drag", () => {
    expect(press({ rowWasSelected: true })).toBe(true);
  });

  test("a press on an unselected row's dead space, size or modified cell sweeps", () => {
    expect(press({})).toBe(false);
  });

  test("the background is never a drag, whatever is selected", () => {
    // No row pressed at all: neither onHandle nor rowWasSelected can be true.
    expect(press({})).toBe(false);
  });

  test("a MODIFIED press never drags, even from the handle or a selected row", () => {
    // Shift/Mod mean "change my selection" — extend the range, toggle this
    // row — and that always gets the additive sweep, never a move: a modifier
    // that also picked up files would be two gestures on one keychord.
    expect(press({ onHandle: true, modified: true })).toBe(false);
    expect(press({ rowWasSelected: true, modified: true })).toBe(false);
    expect(press({ onHandle: true, rowWasSelected: true, modified: true })).toBe(false);
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
  // The gesture as the arbiter sees it: the row pressed, whether the press
  // landed on its handle, and the selection as it stood before the press.
  const gesture = (path: string, selectionBefore: string[], onHandle = false) =>
    pressStartsDrag({
      onHandle,
      rowWasSelected: selectionBefore.includes(path),
      modified: false,
    });

  test("pressing an UNSELECTED row's dead space sweeps, even though the press selects it", () => {
    // The bug, stated as a test. Live, the row is selected a moment after the
    // press and every reading from then on says "move-drag" — which is what a
    // `draggable` attribute is, evaluated when the movement begins rather than
    // when the button went down. From the snapshot the answer is SWEEP, and it
    // stays SWEEP however long the gesture runs.
    const before: string[] = [];
    expect(gesture("/w/notes.md", before)).toBe(false);
    const afterThePress = ["/w/notes.md"];
    expect(
      pressStartsDrag({
        onHandle: false,
        rowWasSelected: afterThePress.includes("/w/notes.md"),
        modified: false,
      }),
    ).toBe(true);
  });

  test("pressing an UNSELECTED row's handle moves just that row", () => {
    // The handle needs no snapshot at all: it is a fact about the DOM at
    // pointerdown, not about a selection the press itself could change.
    expect(gesture("/w/notes.md", [], true)).toBe(true);
  });

  test("pressing a row that WAS selected moves it", () => {
    // Select-then-drag: the second press on the same row is the one that moves
    // it, and this is one of two ways a move-drag can begin (the other is a
    // handle press, which needs no prior selection at all).
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

  test("pressing OUTSIDE a multi-selection, off the handle, sweeps and does not carry it off", () => {
    // The other half of the same press: an unselected row is not part of what
    // is selected, so a dead-space press is a sweep and the old selection is
    // replaced rather than moved.
    const before = ["/w/a.md", "/w/b.md"];
    expect(gesture("/w/z.md", before)).toBe(false);
  });

  test("the background is never a drag, whatever is selected", () => {
    // No row pressed at all: `onHandle` and `rowWasSelected` are false by
    // construction, so the background always sweeps — including with the
    // whole folder selected.
    expect(pressStartsDrag({ onHandle: false, rowWasSelected: false, modified: false })).toBe(
      false,
    );
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

// The post-navigation guard, read the way the CAPTURE-phase arbiter must read
// it: as "this press landed on nothing", not merely "don't start a drag" —
// Listing's own onRowPointerDown does nothing at all for it, and the arbiter
// has to agree or a habitual double-click's second press still moves a file
// nothing selected or highlighted.
describe("pressIsSuppressed", () => {
  test("a row press inside the guard window is suppressed", () => {
    expect(pressIsSuppressed("/w/notes.md", 1_000, 1_100)).toBe(true);
  });

  test("a row press once the window has closed is not suppressed", () => {
    expect(pressIsSuppressed("/w/notes.md", 1_200, 1_100)).toBe(false);
    expect(pressIsSuppressed("/w/notes.md", 1_100, 1_100)).toBe(false);
  });

  test("the background has no row-level guard to honour", () => {
    expect(pressIsSuppressed(null, 1_000, 1_100)).toBe(false);
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
//   • isDir arrives true, because Breadcrumb asserts it rather than probing (a
//     path segment you are standing inside cannot be a file). So "not-a-folder" —
//     and with it the one refusal that owes the user a toast — is unreachable
//     from the strip. That the crumb really does declare "1" is a fact about
//     Breadcrumb.tsx, not something these tests establish;
//   • a crumb is an ANCESTOR of the listed folder and a dragged row is inside
//     it, so "self" and "descendant" are unreachable too — the drop that a
//     crumb makes possible is the one a row cannot offer, moving entries UP.
//     After a spring-load the SAME crumb is the current folder while the dragged
//     rows still come from the deeper one, so it stays an ordinary move up (the
//     first test below is that verdict; nothing here can see the DOM replacement
//     that gets the release to it — refreshDropTarget);
//   • "already-there" is reachable, and it is the one that makes the new
//     per-crumb highlight informative instead of a strip that lights up
//     everywhere: a crumb IS the current folder when the listing is at the root
//     of its tree, and dropping the rows you are looking at into the folder they
//     already live in moves nothing.
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
