import { expect, test } from "bun:test";

import { modeTitle } from "./mode-name";

test("sentinel modes read as their shell names, not as underscore junk", () => {
  expect(modeTitle("_render")).toBe("Rendered");
  expect(modeTitle("_listing")).toBe("Listing");
  expect(modeTitle("_app")).toBe("View");
});

test("the app's own view is called View, in both the pane and the app route", () => {
  // "App" told a user already inside the app nothing. The pane sentinel and
  // the registry mode are the same view from two surfaces, so they share the
  // label — a disagreement here reads as two different things.
  expect(modeTitle("app")).toBe("View");
  expect(modeTitle("_app")).toBe(modeTitle("app"));
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
