import { describe, expect, test } from "bun:test";
import type { Prefs } from "@platform/lib/api";
import {
  GROUPS,
  NAV_SECTIONS,
  SETTINGS,
  navSections,
  resolveGroup,
  sectionsOf,
  visibleGroups,
  visibleSettings,
  type SettingsContext,
} from "./registry";
import { matchedVia, searchSettings as searchTokens, splitLabel, tokenize } from "./search";

const searchSettings = (entries: Parameters<typeof searchTokens>[0], q: string) => searchTokens(entries, tokenize(q));

function ctx(over: Partial<SettingsContext> = {}, prefs: Partial<Prefs> = {}): SettingsContext {
  return {
    bot: false,
    mac: true,
    claudeConfig: false,
    prefs: {
      native_windows: { available: true, enabled: true },
      launcher: { available: true },
      agents: { claude: true, copilot: false, default: "claude" },
      ...prefs,
    } as unknown as Prefs,
    ...over,
  };
}

describe("the registry", () => {
  test("ids are unique and every group is a known one", () => {
    const ids = SETTINGS.map((s) => s.id);
    expect(new Set(ids).size).toBe(ids.length);
    const groupIds = new Set(GROUPS.map((g) => g.id));
    for (const s of SETTINGS) expect(groupIds.has(s.group)).toBe(true);
  });

  test("the three old tab ids still name a group, so old links land", () => {
    for (const tab of ["ai", "indexing", "lan"]) {
      expect(GROUPS.some((g) => g.id === tab)).toBe(true);
    }
  });

  test("every group sits under a nav section, in the sidebar's order", () => {
    const order = NAV_SECTIONS.map((s) => s.id);
    expect(order).toEqual(["general", "bots", "claude", "tasks", "apps", "other"]);
    for (const g of GROUPS) expect(order).toContain(g.nav);
    // Groups are listed section by section — the nav never splits a section.
    const seen = GROUPS.map((g) => order.indexOf(g.nav));
    expect([...seen].sort((a, b) => a - b)).toEqual(seen);
    expect(navSections(GROUPS).map((s) => [s.section.id, s.groups.map((g) => g.id)])).toEqual([
      ["general", ["general", "features"]],
      ["bots", ["ai"]],
      ["claude", ["claude-settings", "claude-plugins", "claude-memory", "claude-mcp", "claude-history"]],
      ["tasks", ["tasks"]],
      ["apps", ["apps", "indexing", "lan"]],
      ["other", ["integrations", "advanced"]],
    ]);
  });

  test("the retired settings are gone", () => {
    const ids = SETTINGS.map((s) => s.id as string);
    for (const gone of ["menubar-pin", "check-updates", "default-model", "phone"]) {
      expect(ids).not.toContain(gone);
    }
  });

  test("a child row always follows a row of its own section", () => {
    SETTINGS.forEach((s, i) => {
      if (!("child" in s)) return;
      expect(i).toBeGreaterThan(0);
      expect(SETTINGS[i - 1].section).toBe(s.section);
    });
  });
});

describe("visibility", () => {
  test("Claude Code is a nav section only where ~/.claude is", () => {
    const without = navSections(visibleGroups(visibleSettings(ctx()))).map((s) => s.section.id);
    expect(without).toEqual(["general", "bots", "tasks", "apps", "other"]);
    const rows = visibleSettings(ctx({ claudeConfig: true }));
    const nav = navSections(visibleGroups(rows));
    expect(nav.map((s) => s.section.id)).toContain("claude");
    // Settings first, then the panel's tab order: Plugins … History last.
    const claude = nav.find((s) => s.section.id === "claude")!.groups;
    expect(claude.map((g) => [g.id, rows.filter((r) => r.group === g.id).map((r) => r.id)])).toEqual([
      ["claude-settings", ["claude-statusline", "claude-catalog"]],
      ["claude-plugins", ["claude-plugins"]],
      ["claude-memory", ["claude-memory", "claude-skills"]],
      ["claude-mcp", ["claude-mcp"]],
      ["claude-history", ["claude-history"]],
    ]);
    // Blocks are one per section: the card is the block.
    for (const r of rows.filter((r) => r.block)) {
      expect(rows.filter((x) => x.group === r.group && x.section === r.section)).toHaveLength(1);
    }
  });

  test("Fused Bot drops the Render-only groups and rows, and its nav drops Apps & files", () => {
    const bot = visibleSettings(ctx({ bot: true }));
    const groups = visibleGroups(bot).map((g) => g.id);
    expect(groups).not.toContain("apps");
    expect(groups).not.toContain("indexing");
    expect(navSections(visibleGroups(bot)).map((s) => s.section.id)).toEqual(["general", "bots", "tasks", "other"]);
    expect(bot.map((s) => s.id)).not.toContain("canvases");
    expect(bot.map((s) => s.id)).not.toContain("fused-account");
    // Tasks and the monitor exist under Bot too.
    expect(groups).toContain("tasks");
    expect(bot.map((s) => s.id)).toContain("monitor");
  });

  test("Community (preview) is one Apps & sharing row, absent on Fused Bot", () => {
    const ids = sectionsOf(visibleSettings(ctx()), "apps")
      .find((s) => s.section === "Community (preview)")
      ?.entries.map((e) => e.id);
    expect(ids).toEqual(["contrib"]);
    expect(visibleSettings(ctx({ bot: true })).some((s) => s.id === "contrib")).toBe(false);
    expect(searchSettings(visibleSettings(ctx()), "proposals inbox").map((s) => s.id)).toEqual(["contrib"]);
  });

  test("Fused Bot's launcher rows speak of bots, in the row and in search", () => {
    const bot = visibleSettings(ctx({ bot: true }));
    const row = bot.find((s) => s.id === "launcher-row")!;
    expect(row.label).toBe("Open the Nth bot");
    expect(bot.find((s) => s.id === "launcher-hotkey")!.description).toContain("your bots");
    expect(searchSettings(bot, "open the nth bot").map((s) => s.id)).toEqual(["launcher-row"]);
    // Render keeps its own words.
    expect(visibleSettings(ctx()).find((s) => s.id === "launcher-row")!.label).toBe("Open the Nth app");
  });

  test("capabilities the server says are missing are not drawn", () => {
    const ids = visibleSettings(ctx({}, { native_windows: undefined, launcher: undefined } as Partial<Prefs>)).map(
      (s) => s.id,
    );
    expect(ids).not.toContain("native-windows");
    expect(ids).not.toContain("launcher-hotkey");
  });


  test("the default-agent picker appears only with both agents on", () => {
    expect(visibleSettings(ctx()).map((s) => s.id)).not.toContain("agent-default");
    const both = ctx({}, { agents: { claude: true, copilot: true, default: "claude" } } as Partial<Prefs>);
    expect(visibleSettings(both).map((s) => s.id)).toContain("agent-default");
  });

  test("sections come out in first-appearance order", () => {
    const secs = sectionsOf(visibleSettings(ctx()), "general").map((s) => s.section);
    expect(secs).toEqual(["Appearance", "Windows", "Keyboard", "Updates", "Status bar", "Accessibility"]);
  });

  test("the Chromium engine row is drawn only where the server says it is available", () => {
    const off = ctx({}, { chromium_windows: { available: false, enabled: false } } as unknown as Partial<Prefs>);
    expect(visibleSettings(off).map((s) => s.id)).not.toContain("chromium-windows");
    expect(visibleSettings(ctx()).map((s) => s.id)).not.toContain("chromium-windows");
    const on = ctx({}, { chromium_windows: { available: true, enabled: false } } as unknown as Partial<Prefs>);
    expect(visibleSettings(on).map((s) => s.id)).toContain("chromium-windows");
  });

  test("Optional features keeps its sections in order, Windows last", () => {
    const on = ctx({}, { chromium_windows: { available: true, enabled: false } } as unknown as Partial<Prefs>);
    const secs = sectionsOf(visibleSettings(on), "features").map((s) => s.section);
    expect(secs).toEqual(["Sidebar", "Windows"]);
    // Without the capability the group is what it was.
    expect(sectionsOf(visibleSettings(ctx()), "features").map((s) => s.section)).toEqual(["Sidebar"]);
  });
});

describe("resolveGroup", () => {
  const groups = visibleGroups(visibleSettings(ctx()));
  test("no tab, the old default and junk all land on General", () => {
    expect(resolveGroup(null, groups)).toBe("general");
    expect(resolveGroup("render", groups)).toBe("general");
    expect(resolveGroup("engines", groups)).toBe("general");
  });
  test("a known tab is kept", () => {
    expect(resolveGroup("indexing", groups)).toBe("indexing");
    expect(resolveGroup("integrations", groups)).toBe("integrations");
  });
  test("the old Accounts tab reads as Integrations", () => {
    expect(resolveGroup("account", groups)).toBe("integrations");
  });
  test("the one Claude Code group's tab lands on Settings, the section's first group", () => {
    const withClaude = visibleGroups(visibleSettings(ctx({ claudeConfig: true })));
    expect(resolveGroup("claude", withClaude)).toBe("claude-settings");
    expect(resolveGroup("claude-settings", withClaude)).toBe("claude-settings");
    // Without ~/.claude there is no such group: General.
    expect(resolveGroup("claude", groups)).toBe("general");
  });
  test("a tab hidden on this flavor falls back", () => {
    const botGroups = visibleGroups(visibleSettings(ctx({ bot: true })));
    expect(resolveGroup("indexing", botGroups)).toBe("general");
  });
});

describe("search", () => {
  const all = visibleSettings(ctx());

  test("tokenize lowercases and splits on whitespace", () => {
    expect(tokenize("  Dark   MODE ")).toEqual(["dark", "mode"]);
    expect(tokenize("   ")).toEqual([]);
  });

  test("an empty query finds nothing (the page shows the group instead)", () => {
    expect(searchSettings(all, "")).toEqual([]);
  });

  test("synonyms find the setting and say which word matched", () => {
    const [hit] = searchSettings(all, "tts");
    expect(hit.id).toBe("reader");
    expect(matchedVia(hit, ["tts"])).toEqual(["tts"]);
    // A word the reader can see in the row is not reported as a synonym.
    expect(matchedVia(hit, ["aloud"])).toEqual([]);
    expect(searchSettings(all, "token").map((h) => h.id)).toEqual(["huggingface"]);

    const dark = searchSettings(all, "dark").map((h) => h.id);
    expect(dark).toContain("appearance");
  });

  test("every word must match", () => {
    expect(searchSettings(all, "git sync").map((h) => h.id)).toEqual(["git-sync"]);
    expect(searchSettings(all, "git nonsenseword")).toEqual([]);
  });

  test("pointers to other pages are findable", () => {
    // The model pointer leads into the Claude Code group, so it needs ~/.claude.
    const rows = visibleSettings(ctx({ claudeConfig: true }));
    expect(searchSettings(rows, "opus").map((h) => h.id)).toEqual(["claude-model"]);
    expect(searchSettings(all, "opus")).toEqual([]);
  });

  test("the Claude blocks are findable by what they hold", () => {
    const rows = visibleSettings(ctx({ claudeConfig: true }));
    expect(searchSettings(rows, "mcp").map((h) => h.id)).toEqual(["claude-mcp", "claude-strict-mcp"]);
    expect(searchSettings(rows, "marketplace").map((h) => h.id).sort()).toEqual(["claude-plugins", "marketplace"]);
  });

  test("results keep page order", () => {
    const ids = searchSettings(all, "sign").map((h) => h.id);
    const order = SETTINGS.map((s) => s.id as string);
    expect([...ids].sort((a, b) => order.indexOf(a) - order.indexOf(b))).toEqual(ids);
  });

  test("the label is split around the first word it contains", () => {
    expect(splitLabel("Low-impact indexing", ["index"])).toEqual({ pre: "Low-impact ", hit: "index", post: "ing" });
    expect(splitLabel("Reader", ["tts"])).toEqual({ pre: "Reader", hit: "", post: "" });
  });
});
