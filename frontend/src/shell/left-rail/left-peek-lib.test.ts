// Left sidebar preview, the parts that need no DOM (left-peek-lib).
import { describe, expect, it } from "bun:test";
import {
  appShow,
  cardPageUrl,
  homeSearchUrl,
  trailBack,
  trailFrameAt,
  trailNoResults,
  trailOpen,
  trailPage,
  trailResults,
  type HomeStep,
  bookmarkFrameSrc,
  canvasShow,
  framePainted,
  frameSrcOf,
  leftFrameLook,
  LEFT_RAIL_SELECTOR,
  leftDoorSelector,
  leftPressVerdict,
  sideOff,
  withQueryEmbed,
  withSearchParam,
} from "./left-peek-lib";

const rawUrl = (p: string) => "/api/fs/raw?path=" + encodeURIComponent(p);
const embedUrl = (p: string, search = "") => "/explorer/embed" + p + search;

describe("frame addresses", () => {
  it("withQueryEmbed adds ?embed=1 once, keeping the query", () => {
    expect(withQueryEmbed("/marketplace")).toBe("/marketplace?embed=1");
    expect(withQueryEmbed("/ai-models/playground?model=x")).toBe("/ai-models/playground?model=x&embed=1");
    expect(withQueryEmbed("/tasks?embed=1")).toBe("/tasks?embed=1");
  });
  it("only the search part changes: a #fragment is kept, a second literal ? stays in the query", () => {
    expect(withQueryEmbed("/tasks#details")).toBe("/tasks?embed=1#details");
    expect(withQueryEmbed("/tasks?view=a#x?y")).toBe("/tasks?view=a&embed=1#x?y");
    expect(withQueryEmbed("/a?q=what?why")).toBe("/a?q=what%3Fwhy&embed=1");
    expect(sideOff("/explorer/embed/x?_mode=code#L3")).toBe("/explorer/embed/x?_mode=code&_side=off#L3");
    expect(sideOff("/explorer/embed/x?_side=off#L3")).toBe("/explorer/embed/x?_side=off#L3");
    expect(withSearchParam("/p", "a", "1")).toBe("/p?a=1");
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

describe("the Home card's trail", () => {
  const home: HomeStep[] = [{ kind: "page", url: "/explorer/embed/Users/me?_side=off" }];
  it("a query puts its results on top once; emptying it takes them away, wherever they are", () => {
    const r = trailResults(home);
    expect(r).toEqual([...home, { kind: "results" }]);
    expect(trailResults(r)).toEqual(r);
    expect(trailNoResults(r)).toEqual(home);
    expect(trailNoResults(home)).toEqual(home);
    // Under a page the results led to, too: Back never lands on empty results.
    const under = trailOpen(r, "/explorer/embed/Users/me/a.pdf");
    expect(trailNoResults(under)).toEqual([...home, { kind: "page", url: "/explorer/embed/Users/me/a.pdf" }]);
  });
  it("a result opens on top; Back walks to the results, then the listing, never past it", () => {
    const t = trailOpen(trailResults(home), "/explorer/embed/Users/me/a.pdf?_side=off");
    expect(trailPage(t)).toBe("/explorer/embed/Users/me/a.pdf?_side=off");
    const r = trailBack(t);
    expect(r[r.length - 1]).toEqual({ kind: "results" });
    expect(trailPage(r)).toBe(home[0].kind === "page" ? home[0].url : "");
    expect(trailBack(trailBack(r))).toEqual(home);
  });
  it("the frame's own hops: a new path is a step, the same path with a new query updates it, none under the results", () => {
    const a = trailFrameAt(home, "/explorer/embed/Users/me/Downloads?_side=off");
    expect(a.length).toBe(2);
    const b = trailFrameAt(a, "/explorer/embed/Users/me/Downloads?_side=off&sel=x");
    expect(b.length).toBe(2);
    expect(trailPage(b)).toBe("/explorer/embed/Users/me/Downloads?_side=off&sel=x");
    expect(trailFrameAt(b, "/explorer/embed/Users/me/Downloads?_side=off&sel=x")).toEqual(b);
    const r = trailResults(b);
    expect(trailFrameAt(r, "/explorer/embed/elsewhere")).toEqual(r);
  });
  it("Open: the framed page as the top page's address; the results as Home with the query", () => {
    expect(cardPageUrl("/explorer/embed/Users/me?_side=off")).toBe("/explorer/view/Users/me");
    expect(cardPageUrl("/explorer/embed/Users/me/a.html?_mode=code&embed=1&_nofocus=1&_preview=1#L3")).toBe("/explorer/view/Users/me/a.html?_mode=code#L3");
    expect(cardPageUrl("/explorer/embed/x?_side=claude")).toBe("/explorer/view/x?_side=claude");
    expect(homeSearchUrl("visa photo")).toBe("/home?q=visa%20photo&qmode=instant");
    expect(homeSearchUrl("  ")).toBe("/home");
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

describe("leftFrameLook: what a card shows while its frame loads", () => {
  const app = appShow({ path: "/w/app", entry: "/w/app/index.html", rawUrl, embedUrl });
  const look = (show: Parameters<typeof leftFrameLook>[0]["show"], o: { stillOk?: boolean; frameKey?: string | null; loaded?: string | null; painted?: string | null } = {}) =>
    leftFrameLook({ show, stillOk: o.stillOk ?? false, frameKey: o.frameKey === undefined ? "1#0#a" : o.frameKey, loaded: o.loaded ?? null, painted: o.painted ?? null });

  it("a canvas, the explorer, a favourite: no spinner, the frame fades in on its load", () => {
    const canvas = canvasShow({ path: "/w/c", embedUrl });
    expect(look(canvas)).toEqual({ ready: false, spinner: false, still: false });
    expect(look(canvas, { loaded: "1#0#a" })).toEqual({ ready: true, spinner: false, still: false });
  });
  it("a slow page (spin) spins until its page has drawn", () => {
    const slow = { kind: "frame" as const, src: "/ai-models?embed=1", spin: true };
    expect(look(slow)).toEqual({ ready: false, spinner: true, still: false });
    expect(look(slow, { loaded: "1#0#a" })).toEqual({ ready: true, spinner: true, still: false });
    expect(look(slow, { loaded: "1#0#a", painted: "1#0#a" })).toEqual({ ready: true, spinner: false, still: false });
  });
  it("a project's still: alone while a preview, then under its live page until that page has drawn", () => {
    expect(look(app, { stillOk: true, frameKey: null })).toEqual({ ready: true, spinner: false, still: true });
    expect(look(app, { stillOk: true, loaded: "1#0#a" })).toEqual({ ready: false, spinner: false, still: true });
    expect(look(app, { stillOk: true, loaded: "1#0#a", painted: "1#0#a" })).toEqual({ ready: true, spinner: false, still: false });
  });
  it("a project with no still (or a failed one): its picture fades in on load, no spinner", () => {
    expect(look(app, { stillOk: false, loaded: "1#0#a" })).toEqual({ ready: true, spinner: false, still: false });
  });
  it("an older frame's load or paint says nothing of the new one", () => {
    expect(look(app, { stillOk: true, frameKey: "2#0#a", loaded: "1#0#a", painted: "1#0#a" })).toEqual({ ready: false, spinner: false, still: true });
  });
});
