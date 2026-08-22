import { expect, test } from "bun:test";
import {
  AI_MODELS_PREFIX,
  AI_MODELS_TABS,
  DEFAULT_TAB,
  isAiModelsPath,
  tabFromPath,
  tabHref,
} from "./routes";

test("every tab round-trips through its own path", () => {
  for (const tab of AI_MODELS_TABS) {
    expect(tabFromPath(tabHref(tab, ""))).toBe(tab);
  }
});

test("the bare prefix reads as the default tab", () => {
  // App.tsx redirects it, but the codec must not depend on that having
  // happened — a render can observe the URL before the rewrite lands.
  expect(tabFromPath(AI_MODELS_PREFIX)).toBe(DEFAULT_TAB);
  expect(tabFromPath(AI_MODELS_PREFIX + "/")).toBe(DEFAULT_TAB);
  expect(DEFAULT_TAB).toBe("playground");
});

test("an unknown sub-path falls back instead of erroring", () => {
  // The forgiving posture the shell takes for an unknown `_mode` (PT-9) and the
  // `?tab=` codec took before it: a stale link should open the page.
  expect(tabFromPath("/ai-models/nonsense")).toBe(DEFAULT_TAB);
  // Including the OLD query-string spelling's path, which is just the bare
  // prefix — this is the whole of the no-legacy-rewrite migration.
  expect(tabFromPath("/ai-models")).toBe(DEFAULT_TAB);
  // This page has no second level.
  expect(tabFromPath("/ai-models/local/deeper")).toBe(DEFAULT_TAB);
});

test("a path off the page is not claimed", () => {
  expect(isAiModelsPath("/ai-models")).toBe(true);
  expect(isAiModelsPath("/ai-models/local")).toBe(true);
  expect(isAiModelsPath("/preferences")).toBe(false);
  // Prefix matching must not swallow a sibling route that merely starts with
  // the same characters.
  expect(isAiModelsPath("/ai-models-extra")).toBe(false);
  // A pathname that is not on the page reads as the default rather than
  // throwing — `tabFromPath` is called with `location.pathname` unguarded.
  expect(tabFromPath("/preferences")).toBe(DEFAULT_TAB);
});

test("the tab strip carries the query across a switch", () => {
  // `?model=` is the playground's selection: switching to Local to unload
  // something and coming back must find the same model selected.
  expect(tabHref("local", "?model=org%2Fname")).toBe("/ai-models/local?model=org%2Fname");
  expect(tabHref("playground", "?cap=text-to-image")).toBe(
    "/ai-models/playground?cap=text-to-image",
  );
});

test("an empty or bare-? search adds no query string", () => {
  // `location.search` is "" with no params; a lone "?" would be a URL that
  // differs from the one the sidebar links to for no reason.
  expect(tabHref("usage", "")).toBe("/ai-models/usage");
  expect(tabHref("usage", "?")).toBe("/ai-models/usage");
});
