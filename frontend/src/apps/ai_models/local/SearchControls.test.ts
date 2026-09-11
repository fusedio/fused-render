// ---- what the reworked controls row OFFERS, pinned against the source -----
// Same discipline as CapabilityPane.test.ts: the ContextMenu-based menu
// surface (never a second hand-rolled dropdown) is a one-line fact a
// screenshot does not distinguish from a hardcoded menu that happens to look
// the same today.
import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const SRC = readFileSync(join(import.meta.dir, "SearchControls.tsx"), "utf8");

describe("SearchControls task menu", () => {
  it("has no Task menu (D843) — the capability the pane was opened for is the only scope now", () => {
    expect(SRC).not.toContain("getHubTasks()");
    expect(SRC).not.toContain('keyLabel="Task:"');
    expect(SRC).not.toContain("Any task");
    expect(SRC).not.toContain("taskItems");
  });
});

describe("SearchControls menu surface", () => {
  it("uses one ControlMenu component for every dropdown, mockup-shaped (item B)", () => {
    expect(SRC).not.toContain("@platform/ui/ContextMenu");
    expect(SRC).toContain('"menubtn" + (active ? " active" : "")');
    expect(SRC).toContain('className="dd" role="menu"');
    const menuCalls = SRC.match(/<ControlMenu/g) ?? [];
    // Fit, Size (params), Sort — three menus in this row now the Task menu
    // is gone (D843).
    expect(menuCalls.length).toBe(3);
  });

  it("shows every option's hover sentence as a <p class=\"h\"> line, not just on the trigger", () => {
    expect(SRC).toContain('<p className="h">{it.hint}</p>');
  });
});

describe("SearchControls result line", () => {
  it("has no unfit toggle or hidden count (item 7, D843 round 5) — every model is always shown", () => {
    expect(SRC).not.toContain("includeUnfit");
    expect(SRC).not.toContain("hiddenUnfit");
    expect(SRC).not.toContain('type="checkbox"');
  });

  it("shows Searching… before a count exists, never a stale one", () => {
    expect(SRC).toContain('loading ? (\n            "Searching…"');
  });

  it("bolds the match count, mockup-style (item E)", () => {
    expect(SRC).toContain("<b>{matchCount}</b> match");
  });
});
