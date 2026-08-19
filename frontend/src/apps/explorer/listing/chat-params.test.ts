// The reproduction these tests encode: chat in the pane about one folder row,
// click a sibling row — a selection change, not a navigation — and the
// conversation followed the click, because `session_id`/`run` live on the shell
// url and nothing took them off it (found by duplicating an app folder and
// switching between the two). The module's job is exactly that removal, and
// only on a genuine retarget: the first target after a page load OR a
// navigation keeps its params, because that is a deep link doing what deep
// links are for.
import { beforeEach, expect, test } from "bun:test";

// router.ts (imported by chat-params) reads `location` at module scope; bun has
// no DOM. Same shim as router.test.ts. The module's own reads/writes go through
// the injected io below instead — the suite shares these globals across files,
// so tests that staged state IN them raced other files' shims.
(globalThis as { location?: unknown }).location ??= {
  pathname: "/",
  search: "",
  href: "http://localhost/",
};
(globalThis as { history?: unknown }).history ??= {
  state: null,
  pushState() {},
  replaceState() {},
};
(globalThis as { window?: unknown }).window ??= {
  dispatchEvent() {},
  setTimeout: globalThis.setTimeout.bind(globalThis),
  clearTimeout: globalThis.clearTimeout.bind(globalThis),
};

const {
  dropStaleChatParams,
  resetChatParamTracking,
  searchWithoutChatParams,
} = await import("./chat-params");

let written: string[] = [];
let search = "";
const io = {
  pathname: () => "/explorer/view/Users/me/apps",
  search: () => search,
  write: (url: string) => written.push(url),
};

beforeEach(() => {
  resetChatParamTracking();
  written = [];
  search = "?_side=claude&session_id=s-1&run=r-1&msg=u-1";
});

// ---- the pure half ----------------------------------------------------------

test("strips exactly the chat's params and keeps everyone else's", () => {
  expect(searchWithoutChatParams("?_side=claude&session_id=a&run=b&msg=c&sel=x"))
    .toBe("?_side=claude&sel=x");
});

test("nothing to strip is null, so the caller can skip the URL write", () => {
  expect(searchWithoutChatParams("?_side=claude&sel=x")).toBeNull();
  expect(searchWithoutChatParams("")).toBeNull();
});

test("a url that was only chat params strips to an empty search", () => {
  expect(searchWithoutChatParams("?session_id=a")).toBe("");
});

// ---- the stateful half ------------------------------------------------------

test("the first target a fresh page shows keeps its params (deep link)", () => {
  dropStaleChatParams("/Users/me/apps/original", false, io);
  expect(written).toEqual([]);
});

test("the same target again is not a retarget", () => {
  dropStaleChatParams("/Users/me/apps/original", false, io);
  dropStaleChatParams("/Users/me/apps/original", false, io);
  expect(written).toEqual([]);
});

test("a changed target takes the chat params off the url", () => {
  dropStaleChatParams("/Users/me/apps/original", false, io);
  dropStaleChatParams("/Users/me/apps/original copy", false, io);
  expect(written).toEqual(["/explorer/view/Users/me/apps?_side=claude"]);
});

test("null holds the tracked target — a Git flip or skeleton is not a retarget", () => {
  dropStaleChatParams("/Users/me/apps/original", false, io);
  dropStaleChatParams(null, false, io);
  dropStaleChatParams("/Users/me/apps/original", false, io);
  expect(written).toEqual([]);
  dropStaleChatParams(null, false, io);
  dropStaleChatParams("/Users/me/apps/original copy", false, io);
  expect(written).toEqual(["/explorer/view/Users/me/apps?_side=claude"]);
});

test("a retarget with nothing on the url writes nothing", () => {
  search = "?_side=claude";
  dropStaleChatParams("/Users/me/apps/original", false, io);
  dropStaleChatParams("/Users/me/apps/original copy", false, io);
  expect(written).toEqual([]);
});

// ---- the navigation reset ---------------------------------------------------
// A navigation (router NAV_EVENT, popstate — both call resetChatParamTracking)
// makes the NEXT target a first sighting: an SPA hop from the Tasks page lands
// with deep-link params for a different folder, and stripping them there was
// the bug the reset exists for.

test("after a navigation the new entry's first target adopts, never strips", () => {
  dropStaleChatParams("/Users/me/apps/original", false, io);
  resetChatParamTracking();
  dropStaleChatParams("/Users/me/other/folder", false, io);
  expect(written).toEqual([]);
});

test("the retarget rule resumes after the post-navigation adoption", () => {
  dropStaleChatParams("/Users/me/apps/original", false, io);
  resetChatParamTracking();
  dropStaleChatParams("/Users/me/other/folder", false, io);
  dropStaleChatParams("/Users/me/other/folder sibling", false, io);
  expect(written).toEqual(["/explorer/view/Users/me/apps?_side=claude"]);
});

// ---- the url-named hop ------------------------------------------------------
// A deep link `?sel=…&session_id=…` mounts the pane on the FOLDER while the
// rows load, then the seeded selection retargets it to the row the link named.
// That hop is the URL playing out, not the user leaving a chat — it adopts. A
// real click is never url-named at the moment it retargets (the `?sel=` mirror
// trails the click), so the strip is untouched.

test("the seeded-selection hop keeps a deep link's params", () => {
  dropStaleChatParams("/Users/me/apps", false, io); // mount: folder, rows loading
  dropStaleChatParams("/Users/me/apps/original", true, io); // ?sel= resolves
  expect(written).toEqual([]);
});

test("after the seeded hop, a real click still strips", () => {
  dropStaleChatParams("/Users/me/apps", false, io);
  dropStaleChatParams("/Users/me/apps/original", true, io);
  dropStaleChatParams("/Users/me/apps/original copy", false, io);
  expect(written).toEqual(["/explorer/view/Users/me/apps?_side=claude"]);
});
