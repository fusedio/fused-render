// The sidebar's ↑/↓ (Projects + Bookmarks rows) and the task peek's ↑/↓ (walk
// the list) meet on the app page's Tasks tab, where the sidebar used to take
// the press from the body first and step a project instead of a task
// (Akshil, 2026-09-21). The rule is pinned at the source: with a peek open,
// the sidebar walks only when focus is INSIDE it.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { expect, test } from "bun:test";

const SRC = readFileSync(join(new URL(".", import.meta.url).pathname, "sidebarArrowNav.ts"), "utf8");

test("an open peek owns the bare arrows unless focus is inside the sidebar", () => {
  expect(SRC).toContain('import { getPeekState } from "@shell/task-peek-store";');
  expect(SRC).toContain("if (!inSidebar && getPeekState().key !== null) return;");
  // …and the check sits BEFORE anything is stepped or the press marked spent.
  expect(SRC.indexOf("getPeekState().key !== null")).toBeLessThan(SRC.indexOf("const links = rowLinks();"));
});
