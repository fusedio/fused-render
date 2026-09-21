// The row catalogue (lib/entry-rows). No DOM, no React renderer, no `location`
// stub: the factories import only the icon set and a type, which is the whole
// reason they take callbacks rather than paths (see the module header).
import { expect, test } from "bun:test";

import {
  allRows,
  claudeCommandRow,
  openWithRow,
  pasteRow,
  renameRow,
} from "@apps/explorer/lib/entry-rows";

test("no two factories spell the same label — one row, one spelling", () => {
  const labels = allRows().map((r) => r.label);
  expect(new Set(labels).size).toBe(labels.length);
});

test("every row carries a glyph, and one label always means one glyph", () => {
  // Two labels MAY share a glyph (Copy Claude session command reuses Open
  // With's, as it always has); one label may not vary its glyph between calls.
  const seen = new Map<string, unknown>();
  for (const r of [...allRows(), ...allRows()]) {
    const icon: unknown = r.icon;
    expect(icon == null).toBe(false);
    const prior = seen.get(r.label);
    if (prior !== undefined) expect(prior === icon).toBe(true);
    seen.set(r.label, icon);
  }
});

test("a factory hands its click straight through", () => {
  const called: string[] = [];
  renameRow(() => called.push("rename")).onClick?.();
  claudeCommandRow(() => called.push("claude")).onClick?.();
  expect(called).toEqual(["rename", "claude"]);
});

test("pasteRow is a listed-but-dead row when nothing is on the clipboard", () => {
  expect(pasteRow(false, () => {}).disabled).toBe(true);
  expect(pasteRow(true, () => {}).disabled).toBe(false);
});

test("openWithRow is a lazy submenu, not a click", () => {
  const row = openWithRow(() => Promise.resolve([]));
  expect(row.onClick).toBeUndefined();
  expect(typeof row.submenu).toBe("function");
});
