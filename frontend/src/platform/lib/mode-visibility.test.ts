import { describe, expect, it } from "bun:test";
import type { TemplateEntry } from "@platform/lib/api";
import { visibleModes, isModePending } from "@platform/lib/mode-visibility";

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

describe("visibleModes", () => {
  it("keeps every unconditional entry, verdicts or not", () => {
    expect(visibleModes([listing, claude], null).map((e) => e.mode)).toEqual(["_listing", "claude"]);
    expect(visibleModes([listing, claude], {}).map((e) => e.mode)).toEqual(["_listing", "claude"]);
  });

  it("shows gated entries while verdicts are still in flight", () => {
    expect(visibleModes([listing, app], null).map((e) => e.mode)).toEqual(["_listing", "app"]);
    expect(isModePending(app, null)).toBe(true);
    expect(isModePending(listing, null)).toBe(false);
  });

  it("drops a gated entry only on an explicit denial", () => {
    expect(visibleModes([listing, app, split], { app: false, claude_split: true }).map((e) => e.mode)).toEqual([
      "_listing",
      "claude_split",
    ]);
  });

  it("keeps gated entries whose verdict never arrived (failed resolveConditions)", () => {
    // A failed call resolves to {} — no verdict for anything. Dropping the
    // entries there would silently empty the menu, so they stay.
    expect(visibleModes([listing, app, split], {}).map((e) => e.mode)).toEqual([
      "_listing",
      "app",
      "claude_split",
    ]);
    expect(isModePending(app, {})).toBe(false);
  });

  it("never strands the active mode, even when denied", () => {
    expect(visibleModes([listing, split], { claude_split: false }).map((e) => e.mode)).toEqual(["_listing"]);
    expect(visibleModes([listing, split], { claude_split: false }, "claude_split").map((e) => e.mode)).toEqual([
      "_listing",
      "claude_split",
    ]);
  });

  it("is order-preserving", () => {
    expect(visibleModes([app, listing, claude], null).map((e) => e.mode)).toEqual([
      "app",
      "_listing",
      "claude",
    ]);
  });
});
