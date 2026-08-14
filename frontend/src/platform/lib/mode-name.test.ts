import { expect, test } from "bun:test";

import { modeTitle } from "./mode-name";

test("sentinel modes read as their shell names, not as underscore junk", () => {
  expect(modeTitle("_render")).toBe("Render");
  expect(modeTitle("_listing")).toBe("Listing");
});

test("an ordinary registry key is capitalized", () => {
  expect(modeTitle("code")).toBe("Code");
  expect(modeTitle("tree")).toBe("Tree");
});

test("a multi-word key humanizes to sentence case, not Title_Case", () => {
  // The mode control now shows the NAME beside the icon, so `claude_split`
  // reaching the user verbatim is a visible defect.
  expect(modeTitle("claude_split")).toBe("Claude split");
  expect(modeTitle("log-studio")).toBe("Log studio");
});

test("keys with a conventional casing keep it", () => {
  expect(modeTitle("duckdb")).toBe("DuckDB");
  expect(modeTitle("geojson")).toBe("GeoJSON");
});

test("the timeline mode is labelled History", () => {
  // It reaches that label through the HUMANIZER, not through NICE_NAMES: the
  // mode's key IS `history` since D243 renamed `versions` into the name (and
  // the clock icon) of the standalone template it replaced. An explicit entry
  // would be a second place to state the same string, and the one it replaced
  // — `versions: "History"` — named a key that no longer exists.
  expect(modeTitle("history")).toBe("History");
});
