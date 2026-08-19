// The reproduction these tests encode: chat in the pane about one folder row,
// click a sibling row — a selection change, not a navigation — and the
// conversation followed the click, because `session_id`/`run` live on the shell
// url and nothing took them off it (found by duplicating an app folder and
// switching between the two). The module's job is exactly that removal, and
// only on a genuine retarget: the first target a fresh page shows keeps its
// params, because that is a deep link doing what deep links are for.
import { beforeEach, expect, test } from "bun:test";

// chat-params.ts reaches for `location` and (through router.ts) `history`; bun
// has no DOM. Same shim as router.test.ts — and grabbed back off globalThis
// after the ??=, so the fields mutated here are the ones the module reads even
// when another test file installed the shim first.
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
const loc = (globalThis as unknown as { location: { pathname: string; search: string } })
  .location;
const hist = (globalThis as unknown as {
  history: { replaceState(s: unknown, t: string, url?: string | null): void };
}).history;

const {
  dropStaleChatParams,
  resetChatParamTracking,
  searchWithoutChatParams,
} = await import("./chat-params");

let written: string[] = [];
hist.replaceState = (_s: unknown, _t: string, url?: string | null) => {
  if (typeof url === "string") written.push(url);
};

beforeEach(() => {
  resetChatParamTracking();
  written = [];
  loc.pathname = "/explorer/view/Users/me/apps";
  loc.search = "?_side=claude&session_id=s-1&run=r-1&msg=u-1";
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
  dropStaleChatParams("/Users/me/apps/original");
  expect(written).toEqual([]);
});

test("the same target again is not a retarget", () => {
  dropStaleChatParams("/Users/me/apps/original");
  dropStaleChatParams("/Users/me/apps/original");
  expect(written).toEqual([]);
});

test("a changed target takes the chat params off the url", () => {
  dropStaleChatParams("/Users/me/apps/original");
  dropStaleChatParams("/Users/me/apps/original copy");
  expect(written).toEqual(["/explorer/view/Users/me/apps?_side=claude"]);
});

test("null holds the tracked target — a Git flip or skeleton is not a retarget", () => {
  dropStaleChatParams("/Users/me/apps/original");
  dropStaleChatParams(null);
  dropStaleChatParams("/Users/me/apps/original");
  expect(written).toEqual([]);
  dropStaleChatParams(null);
  dropStaleChatParams("/Users/me/apps/original copy");
  expect(written).toEqual(["/explorer/view/Users/me/apps?_side=claude"]);
});

test("a retarget with nothing on the url writes nothing", () => {
  loc.search = "?_side=claude";
  dropStaleChatParams("/Users/me/apps/original");
  dropStaleChatParams("/Users/me/apps/original copy");
  expect(written).toEqual([]);
});
