// ---- what the capability column SAYS, pinned against the source ----------
// Same discipline as ModelRow.test.ts: this file's states (hover,
// off, zero) are one-line facts a screenshot does not distinguish from their
// near neighbours, so they are pinned by reading the source rather than by
// rendering it.
import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const NAV = readFileSync(join(import.meta.dir, "CapabilityNav.tsx"), "utf8");

describe("CapabilityNav", () => {
  it("is transparent — no background fill of its own beyond the CSS class", () => {
    expect(NAV).toContain('className="tp-nav"');
    expect(NAV).toContain('<h5>What this Mac can do</h5>');
  });

  it("hints Nothing downloaded yet for a zero count and n on this Mac otherwise", () => {
    expect(NAV).toContain('"Nothing downloaded yet"');
    expect(NAV).toContain("`${count} on this Mac`");
  });

  it("hints Not available on this device with the registry's own reason when off", () => {
    expect(NAV).toContain("`Not available on this device: ${offReason");
  });

  it("marks an off row n/a rather than a zero count", () => {
    expect(NAV).toContain('off ? "n/a" : count || 0');
  });

  it("treats a capability with no catalog entry as available, not off", () => {
    expect(NAV).toContain("entry.runner !== null && !entry.runner.available");
  });

  it("renders the rule divider between capabilities and the Engine files row", () => {
    expect(NAV).toContain('<div className="rule" />');
  });

  it("gives the Engine files row its own icon, title and nav key", () => {
    expect(NAV).toContain('navKey="parts"');
    expect(NAV).toContain('title="Engine files"');
    expect(NAV).toContain("icon={PARTS_ICON}");
  });

  it("marks the selected row with the on class", () => {
    expect(NAV).toContain('className={`${selected ? "on" : ""}${off ? " off" : ""}`}');
  });
});
