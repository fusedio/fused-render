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
    // Item 6 (fix round 6): the dropdown's class is now conditional on
    // `align` (`.dd.right` for the Sort menu) rather than a hardcoded
    // literal — assert the anchoring logic exists instead of the old fixed
    // string.
    expect(SRC).toContain('"dd" + (align === "right" ? " right" : "")');
    const menuCalls = SRC.match(/<ControlMenu/g) ?? [];
    // Fit, Size (params), Sort — three ControlMenu-based menus in this row
    // now the Task menu is gone (D843); Publisher/Quant are `SearchMenu`
    // now (item 5, round 6), not `ControlMenu`.
    expect(menuCalls.length).toBe(3);
    // Item 6: the Sort menu (the row's right-most trigger, after the `.push`
    // spacer) anchors its dropdown to the right so it can't run past the
    // scrolling pane's edge.
    expect(SRC).toContain('align="right"');
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

// Item 5 (fix round 6): Publisher/Quant free-text inputs became searchable
// dropdowns (`SearchMenu`) fed by the server's `facets`. This is the same
// source-pinning discipline as the rest of the file — there is no
// React-rendering test setup wired into this package's `bun test` — so the
// three behaviors the brief calls out (narrows on typing, Enter applies free
// text, picking an option settles) are asserted against the actual narrowing
// predicate, Enter handler, and click handler in the source, not just their
// presence.
describe("SearchControls Publisher/Quant menus (item 5)", () => {
  it("replaced the two free-text inputs with SearchMenu, fed by facets", () => {
    expect(SRC).not.toContain('className="am-hub-textfilter"');
    expect(SRC).toContain('keyLabel="Quant:"');
    expect(SRC).toContain('keyLabel="Publisher:"');
    expect(SRC).toContain("options={facets?.quants ?? []}");
    expect(SRC).toContain("options={facets?.publishers ?? []}");
  });

  it("narrows the option list by a case-insensitive substring match on the typed filter", () => {
    expect(SRC).toContain(
      "const narrowed = filter\n    ? options.filter((o) => o.id.toLowerCase().includes(filter.toLowerCase()))\n    : options;",
    );
  });

  it("applies typed free text on Enter even when it matches no option", () => {
    expect(SRC).toContain('if (e.key === "Enter" && filter.trim())');
    expect(SRC).toContain("apply(filter.trim())");
  });

  it("picking a listed option calls the settle handler (onChange) with its id", () => {
    expect(SRC).toContain("onClick={() => apply(o.id)}");
    // `apply` is the one path both Enter and a click go through, and it is
    // the function that actually calls the `onChange` prop threaded in as
    // `onQuant`/`onPublisher` from `HubSearchScreen`.
    expect(SRC).toContain("const apply = (v: string) => {\n    onChange(v);\n    setOpen(false);\n  };");
  });

  it("clearing (the trigger's × ) settles an empty value, same contract as ControlMenu's onClear", () => {
    expect(SRC).toContain("apply(\"\");");
  });
});
