// ---- what the reworked controls row OFFERS, pinned against the source -----
// Same discipline as CapabilityPane.test.ts: the D313 server-driven task
// list and the ContextMenu-based menu surface (never a second hand-rolled
// dropdown) are one-line facts a screenshot does not distinguish from a
// hardcoded menu that happens to look the same today.
import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const SRC = readFileSync(join(import.meta.dir, "SearchControls.tsx"), "utf8");

describe("SearchControls task vocabulary", () => {
  it("asks the server for the task glossary rather than hardcoding one (D313)", () => {
    expect(SRC).toContain("getHubTasks()");
    expect(SRC).not.toContain('"text-generation"');
  });
});

describe("SearchControls menu surface", () => {
  it("reuses the app's one ContextMenu-based ControlMenu for every dropdown", () => {
    expect(SRC).toContain("import ContextMenu, { type MenuEntry } from \"@platform/ui/ContextMenu\";");
    const menuCalls = SRC.match(/<ControlMenu/g) ?? [];
    // Task, Fit, Size (params), Sort — four menus in this row.
    expect(menuCalls.length).toBe(4);
  });
});

describe("SearchControls result line", () => {
  it("states a hidden-unfit count only while the toggle is off", () => {
    expect(SRC).toContain("hiddenUnfit > 0 && !includeUnfit");
  });

  it("shows Searching… before a count exists, never a stale one", () => {
    expect(SRC).toContain('loading\n            ? "Searching…"');
  });
});
