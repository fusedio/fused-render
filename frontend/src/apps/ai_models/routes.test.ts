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

test("the strip is Playground / Local / Benchmark / Engines / Usage, and Discover is gone", () => {
  // The order IS the strip's order and the first entry IS the default, so this
  // pins the one list that decides both.
  expect([...AI_MODELS_TABS]).toEqual(["playground", "local", "benchmark", "engines", "usage"]);
});

test("Benchmark sits directly after Local", () => {
  // Position is the argument: Benchmark is about the models the Local tab
  // lists, so it reads as the next question about them ("how fast are these")
  // rather than as a sibling of Engines (which is about backends) or of Usage
  // (which is about what this process happened to do).
  const strip = [...AI_MODELS_TABS];
  expect(strip.indexOf("benchmark")).toBe(strip.indexOf("local") + 1);
  // And it is not the default: an empty machine still lands on the playground,
  // because a benchmark needs a model that is already downloaded.
  expect(DEFAULT_TAB).not.toBe("benchmark");
});

test("/ai-models/benchmark resolves to the tab instead of falling back", () => {
  // The failure this guards is silent: an unrouted path falls back to the
  // default, so a tab added to the strip but not to the union renders the
  // playground under a Benchmark-looking URL rather than erroring.
  expect(tabFromPath("/ai-models/benchmark")).toBe("benchmark");
  expect(tabFromPath("/ai-models/benchmark")).not.toBe(DEFAULT_TAB);
});

test("the benchmark tab keeps the query string across a switch", () => {
  // `?cap=` is the Benchmark tab's own focus filter as well as the
  // playground's seed, so it has to survive a switch in both directions.
  expect(tabHref("benchmark", "?cap=text-to-image")).toBe(
    "/ai-models/benchmark?cap=text-to-image",
  );
  expect(tabHref("benchmark", "")).toBe("/ai-models/benchmark");
});

test("the unrouted discover path lands on the default like any stale link", () => {
  // The Local tab answers Discover's question now (D423), so it left the strip.
  // The code did not leave the tree, and the point of THIS test is that its old
  // address behaves like any other unknown sub-path rather than erroring or
  // reaching a tab nothing links to.
  expect(tabFromPath("/ai-models/discover")).toBe(DEFAULT_TAB);
  expect((AI_MODELS_TABS as readonly string[]).includes("discover")).toBe(false);
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
