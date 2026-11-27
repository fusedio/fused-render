// Left sidebar preview, the parts that need no DOM (left-peek-lib).
import { describe, expect, it } from "bun:test";
import {
  SEARCH_CARD_SRC,
  appShow,
  bookmarkFrameSrc,
  canvasShow,
  framePainted,
  frameSrcOf,
  LEFT_RAIL_SELECTOR,
  leftDoorSelector,
  leftPressVerdict,
  searchHopTarget,
  withQueryEmbed,
} from "./left-peek-lib";

const rawUrl = (p: string) => "/api/fs/raw?path=" + encodeURIComponent(p);
const embedUrl = (p: string, search = "") => "/explorer/embed" + p + search;

describe("frame addresses", () => {
  it("withQueryEmbed adds ?embed=1 once, keeping the query", () => {
    expect(withQueryEmbed("/marketplace")).toBe("/marketplace?embed=1");
    expect(withQueryEmbed("/ai-models/playground?model=x")).toBe("/ai-models/playground?model=x&embed=1");
    expect(withQueryEmbed("/tasks?embed=1")).toBe("/tasks?embed=1");
  });
  it("the search card is Home narrowed to its search, embedded", () => {
    expect(SEARCH_CARD_SRC).toBe("/home?embed=1&searchonly=1");
  });
  it("a favourite: an explorer page's own embed, a shell route embedded, anything else no card", () => {
    expect(bookmarkFrameSrc("/explorer/view/Users/me/a.html?_mode=code")).toBe("/explorer/embed/Users/me/a.html?_mode=code&_side=off");
    expect(bookmarkFrameSrc("/explorer/embed/Users/me")).toBe("/explorer/embed/Users/me?_side=off");
    // A companion pane the bookmark asked for stays shut in the card.
    expect(bookmarkFrameSrc("/explorer/view/Users/me?_side=claude")).toBe("/explorer/embed/Users/me?_side=off");
    expect(bookmarkFrameSrc("/tasks")).toBe("/tasks?embed=1");
    expect(bookmarkFrameSrc("https://example.com")).toBeNull();
    expect(bookmarkFrameSrc("//example.com/x")).toBeNull();
    expect(bookmarkFrameSrc("s3://bucket/key")).toBeNull();
    expect(bookmarkFrameSrc("/api/fs/raw?path=x")).toBeNull();
    expect(bookmarkFrameSrc("")).toBeNull();
  });
  it("a project: its preview.png, its entry page as a picture (thumbnail stamp), and the real page — Claude's pane shut in both", () => {
    expect(appShow({ path: "/w/app/", entry: "/w/app/index.html", rawUrl, embedUrl })).toEqual({
      kind: "app",
      still: rawUrl("/w/app/preview.png"),
      picture: "/explorer/embed/w/app/index.html?_side=off&_preview=1",
      live: "/explorer/embed/w/app/index.html?_side=off",
    });
    // No entry page: the folder, its listing without the side pane.
    expect(appShow({ path: "/w/app", entry: null, rawUrl, embedUrl })).toMatchObject({
      picture: "/explorer/embed/w/app?_side=off&_preview=1",
      live: "/explorer/embed/w/app?_side=off",
    });
  });
  it("a canvas: its clone's canvas.toml in the canvas view, fitted", () => {
    expect(canvasShow({ path: "/c/prod/me/demo/", embedUrl })).toEqual({ kind: "frame", src: "/explorer/embed/c/prod/me/demo/canvas.toml?_mode=canvas&fit=1" });
  });
  it("frameSrcOf: a pinned project frames its REAL page; a preview its picture only when the still failed, else none", () => {
    const app = appShow({ path: "/w/a", entry: "/w/a/i.html", rawUrl, embedUrl });
    if (app.kind !== "app") throw new Error("an app");
    expect(frameSrcOf(app, { pinned: false, stillFailed: false })).toBeNull();
    expect(frameSrcOf(app, { pinned: false, stillFailed: true })).toBe(app.picture);
    expect(frameSrcOf(app, { pinned: true, stillFailed: false })).toBe(app.live);
    expect(frameSrcOf(app, { pinned: true, stillFailed: true })).toBe(app.live);
    expect(app.live).not.toContain("_preview");
    expect(app.live).not.toContain("_nofocus");
    expect(frameSrcOf({ kind: "frame", src: "/x" }, { pinned: false, stillFailed: false })).toBe("/x");
  });
});

describe("searchHopTarget", () => {
  it("still on its search: nothing to open", () => {
    expect(searchHopTarget("/home?embed=1&searchonly=1")).toBeNull();
    expect(searchHopTarget("/home?embed=1&searchonly=1&q=report")).toBeNull();
  });
  it("a result: the top page's address — the view prefix, the frame's stamps dropped", () => {
    expect(searchHopTarget("/explorer/embed/Users/me/report.pdf?embed=1&searchonly=1")).toBe("/explorer/view/Users/me/report.pdf");
    expect(searchHopTarget("/explorer/embed/Users/me?sel=a.txt&_nofocus=1&_preview=1")).toBe("/explorer/view/Users/me?sel=a.txt");
    expect(searchHopTarget("/preferences?tab=indexing")).toBe("/preferences?tab=indexing");
  });
});

/** An element stand-in that matches the selectors given. */
function at(...matches: string[]) {
  return { closest: (s: string) => (matches.includes(s) ? {} : null) };
}

describe("leftPressVerdict: what a press is to a left card", () => {
  const card = "#left-rail-peek-1-home";
  const own = leftDoorSelector("home");
  const o = { pressedInside: false, cardSelectors: [card], ownSelectors: [own] };
  it("the card, its bridge, what it opened: inside", () => {
    expect(leftPressVerdict(at(card), o)).toBe("inside");
    expect(leftPressVerdict(at(".claude-rail-peek-bridge.is-left"), o)).toBe("inside");
    expect(leftPressVerdict(at(), { ...o, pressedInside: true })).toBe("inside");
  });
  it("its own icon: own", () => {
    expect(leftPressVerdict(at(own, LEFT_RAIL_SELECTOR), o)).toBe("own");
  });
  it("the rest of the sidebar (collapsed or expanded, its seam) and the other left cards: the view", () => {
    expect(LEFT_RAIL_SELECTOR).toBe("#sidebar, .sidebar-resize-handle");
    expect(leftPressVerdict(at(LEFT_RAIL_SELECTOR), o)).toBe("view");
    expect(leftPressVerdict(at(".claude-rail-peek.is-left"), o)).toBe("view");
  });
  it("the page, a frame, and the CLAUDE RAIL and its cards: outside", () => {
    expect(leftPressVerdict(at(), o)).toBe("outside");
    expect(leftPressVerdict(null, o)).toBe("outside");
    expect(leftPressVerdict(at(".claude-rail"), o)).toBe("outside");
    expect(leftPressVerdict(at(".claude-rail-peek"), o)).toBe("outside");
    expect(leftPressVerdict(at(".claude-rail-peek-bridge"), o)).toBe("outside");
  });
  it("the icon selector quotes its key", () => {
    expect(leftDoorSelector('pin:a"b')).toBe('[data-left-door="pin:a\\"b"]');
  });
  it("the + New app card is the composer itself: no frame", () => {
    expect(frameSrcOf({ kind: "compose" }, { pinned: false, stillFailed: false })).toBeNull();
    expect(frameSrcOf({ kind: "compose" }, { pinned: true, stillFailed: true })).toBeNull();
  });
  it("a framed page has painted once its shell drew, nothing resolves, and a preview's frame is the shown one", () => {
    const doc = (present: string[]) => ({ querySelector: (sel: string) => (sel.split(",").some((s) => present.includes(s.trim())) ? {} : null) });
    expect(framePainted(null)).toBe(false);
    // The shell's root is still empty.
    expect(framePainted(doc([]))).toBe(false);
    // A page with no preview body (AI Models, a folder's listing).
    expect(framePainted(doc(["#root > *"]))).toBe(true);
    // A view still resolving.
    expect(framePainted(doc(["#root > *", ".preview-resolving"]))).toBe(false);
    // A file's frame loading, then shown.
    expect(framePainted(doc(["#root > *", ".preview-body iframe"]))).toBe(false);
    expect(framePainted(doc(["#root > *", ".preview-body iframe", ".preview-body iframe.is-shown"]))).toBe(true);
  });
});
