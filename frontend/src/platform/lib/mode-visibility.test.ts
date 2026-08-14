import { describe, expect, it } from "bun:test";
import type { TemplateEntry } from "@platform/lib/api";
import {
  visibleModes,
  isModePending,
  isSidebarMode,
  partitionModes,
  orderSidebarModes,
  defaultSidebarMode,
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
const git = t("git", true);
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

describe("partitionModes", () => {
  const image = t("image");

  it("splits the companions out and preserves order in both halves", () => {
    const { content, sidebar } = partitionModes([image, t("photos"), claude, git]);
    expect(names(content)).toEqual(["image", "photos"]);
    expect(names(sidebar)).toEqual(["claude", "git"]);
  });

  it("leaves a list with no companions entirely to the content pane", () => {
    const { content, sidebar } = partitionModes([image, listing]);
    expect(names(content)).toEqual(["image", "_listing"]);
    expect(sidebar).toEqual([]);
  });

  it("agrees with isSidebarMode", () => {
    expect(isSidebarMode("claude")).toBe(true);
    expect(isSidebarMode("git")).toBe(true);
    expect(isSidebarMode("claude_split")).toBe(false);
    expect(isSidebarMode("_render")).toBe(false);
  });

  // `git` is on SIDEBAR_MODES but never in a FILE's own template list: the
  // registry keeps it on the universal "/" directory key and its gate refuses a
  // file, so a file's partition can only ever see one if a user registry rebinds
  // it. The file sidebar's own git entry is BORROWED from the parent folder and
  // appended (lib/dir-mode), which is why `orderSidebarModes` exists.
  it("does not invent a git entry for a file that has none", () => {
    const { content, sidebar } = partitionModes([image, claude]);
    expect(names(content)).toEqual(["image"]);
    expect(names(sidebar)).toEqual(["claude"]);
  });

  it("takes a file's own git entry into the sidebar half when there is one", () => {
    // A user registry may bind `git` to a file extension; it is still a companion.
    const { content, sidebar } = partitionModes([image, git, claude]);
    expect(names(content)).toEqual(["image"]);
    expect(names(sidebar)).toEqual(["git", "claude"]);
  });

  it("defaults the sidebar to the chat, whatever the registry's order was", () => {
    // The registry ranks views for a FILE TYPE; this is a preference between the
    // companions, so SIDEBAR_MODES order wins over the list's.
    expect(defaultSidebarMode([git, claude])).toBe("claude");
    expect(defaultSidebarMode([git])).toBe("git");
    // An unranked companion (a user registry's own) still opens something.
    expect(defaultSidebarMode([t("my_notes")])).toBe("my_notes");
    expect(defaultSidebarMode([])).toBe(null);
  });
});

// The switcher's order is Claude / Git for every file, because the list is
// ASSEMBLED (git arrives from the parent folder and is appended) rather than
// read off one registry key.
describe("orderSidebarModes", () => {
  it("puts the companions in SIDEBAR_MODES order, not the input's", () => {
    // The shape the file sidebar actually builds: the file's own companions in
    // registry order, then the borrowed git appended at the end — and the
    // reverse, which is the case the rank exists for.
    expect(names(orderSidebarModes([claude, git]))).toEqual(["claude", "git"]);
    expect(names(orderSidebarModes([git, claude]))).toEqual(["claude", "git"]);
  });

  it("leaves an unknown companion at the end, in the order it came", () => {
    const mine = t("my_notes");
    const yours = t("your_notes");
    expect(names(orderSidebarModes([mine, git, yours, claude]))).toEqual([
      "claude",
      "git",
      "my_notes",
      "your_notes",
    ]);
  });

  it("does not mutate its input", () => {
    const input = [git, claude];
    orderSidebarModes(input);
    expect(names(input)).toEqual(["git", "claude"]);
  });
});

describe("defaultMode", () => {
  it("prefers the first unconditional entry", () => {
    expect(defaultMode([app, listing, claude])?.mode).toBe("_listing");
  });

  it("keeps a folder browsable when the registry leads with a GATED mode", () => {
    // The safety net under D278's reorder. The universal `/` key now ships
    // `["claude", "_listing", …]` so the preview PANE lands on the chat
    // (`activePaneMode` takes modes[0] literally). The FULL-SCREEN folder route
    // resolves through here instead, and "first unconditional" makes `_listing`
    // win from second place — which is why the reorder cannot leave a folder
    // opening as a chat with no file table. If this ever changes, opening any
    // folder breaks, so it is pinned in the shape the registry actually ships.
    // The registry's `claude` is condition.py-gated, unlike the bare fixture.
    expect(defaultMode([t("claude", true), listing])?.mode).toBe("_listing");
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
