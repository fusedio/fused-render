import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";

// Cards are always one horizontal row whatever the tile height; only the icons
// strip and list format follow the tile's row count.
const strip = readFileSync(new URL("./widgets/StripWidgets.tsx", import.meta.url), "utf8");
const apps = readFileSync(new URL("./widgets/AppsWidget.tsx", import.meta.url), "utf8");

test("StripWidgets cards never derive rows from the tile size", () => {
  expect(strip).not.toMatch(/dims\(widget\.size\)\.rows/);
  expect(strip.match(/<CardStrip count=\{count\} rows=\{1\}/g)?.length).toBe(3);
  expect(strip).not.toMatch(/\* rows/);
});

test("AppsWidget: cards strip is one row, icons strip keeps the tile rows", () => {
  expect(apps).toMatch(/<CardStrip count=\{count\} rows=\{1\}/);
  expect(apps).toMatch(/variant="icons" count=\{count\} rows=\{rows\}/);
  expect(apps).toMatch(/const cap = cards \? \(count \?\? 0\) :/);
});
