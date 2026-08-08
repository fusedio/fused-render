import { describe, expect, it } from "bun:test";
import type { TemplateEntry } from "@platform/lib/api";
import {
  visibleModes,
  isModePending,
  defaultMode,
  effectiveActive,
} from "@platform/lib/mode-visibility";

const t = (mode: string, conditional = false): TemplateEntry => ({
  mode,
  path: conditional ? `/tpl/${mode}/template.html` : null,
  icon: null,
  ...(conditional ? { conditional: true } : {}),
});

const listing = t("_listing");
const claude = t("claude");
const app = t("app", true);
const split = t("claude_split", true);

const names = (entries: TemplateEntry[]) => entries.map((e) => e.mode);

describe("visibleModes", () => {
  it("keeps every unconditional entry, verdicts or not", () => {
    expect(names(visibleModes([listing, claude], null))).toEqual(["_listing", "claude"]);
    expect(names(visibleModes([listing, claude], {}))).toEqual(["_listing", "claude"]);
  });

  it("shows gated entries while verdicts are still in flight", () => {
    expect(names(visibleModes([listing, app], null))).toEqual(["_listing", "app"]);
    expect(isModePending(app, null)).toBe(true);
    expect(isModePending(listing, null)).toBe(false);
  });

  it("drops a gated entry only on an explicit denial", () => {
    expect(names(visibleModes([listing, app, split], { app: false, claude_split: true }))).toEqual([
      "_listing",
      "claude_split",
    ]);
  });

  it("keeps gated entries whose verdict never arrived (failed resolveConditions)", () => {
    // A failed call resolves to {} — no verdict for anything. Dropping the
    // entries there would silently empty the menu, so they stay.
    expect(names(visibleModes([listing, app, split], {}))).toEqual([
      "_listing",
      "app",
      "claude_split",
    ]);
    expect(isModePending(app, {})).toBe(false);
  });

  it("is order-preserving", () => {
    expect(names(visibleModes([app, listing, claude], null))).toEqual(["app", "_listing", "claude"]);
  });
});

describe("defaultMode", () => {
  it("prefers the first unconditional entry", () => {
    expect(defaultMode([app, listing, claude])?.mode).toBe("_listing");
  });

  it("falls back to the first entry of an all-conditional list", () => {
    expect(defaultMode([split, app])?.mode).toBe("claude_split");
  });

  it("is null for an empty list", () => {
    expect(defaultMode([])).toBe(null);
  });
});

describe("effectiveActive", () => {
  it("honours a request that is still on offer", () => {
    const visible = visibleModes([listing, split], { claude_split: true });
    expect(effectiveActive(visible, "claude_split")?.mode).toBe("claude_split");
  });

  it("falls back to the default when the request was DENIED, and hides it", () => {
    // The bug this encodes: pinning a denied-but-active entry into the menu
    // left it at one entry (so ModeMenu hid) with the view stuck on a mode the
    // gate refused. The active mode moves instead.
    const visible = visibleModes([listing, split], { claude_split: false });
    expect(names(visible)).toEqual(["_listing"]);
    expect(effectiveActive(visible, "claude_split")?.mode).toBe("_listing");
  });

  it("keeps a requested mode whose verdict never arrived", () => {
    const visible = visibleModes([listing, split], {});
    expect(names(visible)).toEqual(["_listing", "claude_split"]);
    expect(effectiveActive(visible, "claude_split")?.mode).toBe("claude_split");
  });

  it("keeps a requested mode while its verdict is in flight", () => {
    const visible = visibleModes([listing, split], null);
    expect(effectiveActive(visible, "claude_split")?.mode).toBe("claude_split");
  });

  it("falls back for an unknown or absent request", () => {
    const visible = visibleModes([listing, claude], {});
    expect(effectiveActive(visible, "no_such_mode")?.mode).toBe("_listing");
    expect(effectiveActive(visible, null)?.mode).toBe("_listing");
  });

  it("leaves nothing active when every mode is denied", () => {
    // The caller (Preview) then renders its own fallback — an empty visible
    // list must not resolve to some phantom entry.
    const visible = visibleModes([app, split], { app: false, claude_split: false });
    expect(visible).toEqual([]);
    expect(effectiveActive(visible, "app")).toBe(null);
  });

  it("always resolves INTO the visible list, whatever was requested", () => {
    // The invariant Preview's render path leans on: the entry it renders this
    // paint is always one the visible list still offers, so the held-frame
    // swap can key off it directly instead of waiting for a state effect to
    // catch up (which spent a paint on a frame for a dropped mode).
    const entries = [listing, claude, app, split];
    const verdictSets: Array<Record<string, boolean> | null> = [
      null,
      {},
      { app: false, claude_split: false },
      { app: true, claude_split: false },
    ];
    for (const verdicts of verdictSets) {
      const visible = visibleModes(entries, verdicts);
      for (const requested of ["app", "claude_split", "nope", null]) {
        const active = effectiveActive(visible, requested);
        expect(active === null || visible.includes(active)).toBe(true);
      }
    }
  });

  it("single visible entry after a denial means the menu is correctly absent", () => {
    // ModeMenu hides at <=1 entry: with the denied mode gone, the ONE
    // remaining mode is also the effective active one, so there is genuinely
    // nothing to choose between.
    const visible = visibleModes([listing, split], { claude_split: false });
    expect(visible.length).toBe(1);
    expect(effectiveActive(visible, "claude_split")?.mode).toBe(visible[0].mode);
  });
});
