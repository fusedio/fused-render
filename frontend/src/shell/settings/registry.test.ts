import { describe, expect, test } from "bun:test";
import type { Prefs } from "@platform/lib/api";
import {
  GROUPS,
  NAV_SECTIONS,
  SETTINGS,
  navSections,
  resolveGroup,
  sectionsOf,
  unavailableReason,
  visibleGroups,
  visibleSettings,
  type DeviceFeature,
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
      agents: { claude: true, copilot: false, codex: false, gemini: false, default: "claude" },
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
    expect(order).toEqual(["app", "agents", "apps", "developer"]);
    for (const g of GROUPS) expect(order).toContain(g.nav);
    // Groups are listed section by section — the nav never splits a section.
    const seen = GROUPS.map((g) => order.indexOf(g.nav));
    expect([...seen].sort((a, b) => a - b)).toEqual(seen);
    expect(navSections(GROUPS).map((s) => [s.section.id, s.groups.map((g) => g.id)])).toEqual([
      ["app", ["general", "integrations", "features"]],
      ["agents", ["ai", "tasks", "claude-cli"]],
      ["apps", ["apps", "indexing", "lan"]],
      ["developer", ["advanced"]],
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
  test("the nav has no Claude Code section; it is the Claude Code page", () => {
    const nav = navSections(visibleGroups(visibleSettings(ctx({ claudeConfig: true })))).map((s) => s.section.id);
    expect(nav).toEqual(["app", "agents", "apps", "developer"]);
  });

  test("Fused Bot drops the Render-only groups and rows, and its nav drops Apps & files", () => {
    const bot = visibleSettings(ctx({ bot: true }));
    const groups = visibleGroups(bot).map((g) => g.id);
    expect(groups).not.toContain("apps");
    expect(groups).not.toContain("indexing");
    expect(navSections(visibleGroups(bot)).map((s) => s.section.id)).toEqual(["app", "agents", "developer"]);
    expect(bot.map((s) => s.id)).not.toContain("canvases");
    expect(bot.map((s) => s.id)).not.toContain("fused-account");
    // Tasks and the monitor exist under Bot too.
    expect(groups).toContain("tasks");
    expect(bot.map((s) => s.id)).toContain("monitor");
  });

  test("Community (preview) is the loop row then Remix under Apps & sharing, absent on Fused Bot", () => {
    const ids = sectionsOf(visibleSettings(ctx()), "apps")
      .find((s) => s.section === "Community (preview)")
      ?.entries.map((e) => e.id);
    expect(ids).toEqual(["contrib", "contrib-remix"]);
    expect(visibleSettings(ctx({ bot: true })).some((s) => s.id.startsWith("contrib"))).toBe(false);
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
    const ids = visibleSettings(ctx({}, { launcher: undefined } as Partial<Prefs>)).map((s) => s.id);
    expect(ids).not.toContain("launcher-hotkey");
  });


  test("the embed proxy switch appears only while Canvases is on, and never in Fused Bot", () => {
    const ids = (enabled: boolean, over: Partial<SettingsContext> = {}) =>
      visibleSettings(ctx(over, { canvases: { enabled } } as Partial<Prefs>)).map((s) => s.id);
    expect(visibleSettings(ctx()).map((s) => s.id)).not.toContain("canvases-embed-proxy");
    expect(ids(false)).not.toContain("canvases-embed-proxy");
    expect(ids(true)).toContain("canvases-embed-proxy");
    expect(ids(true, { bot: true })).not.toContain("canvases-embed-proxy");
  });

  test("the default-agent picker appears only with two or more agents on", () => {
    expect(visibleSettings(ctx()).map((s) => s.id)).not.toContain("agent-default");
    const agents = (claude: boolean, copilot: boolean, codex: boolean, gemini = false) =>
      visibleSettings(ctx({}, { agents: { claude, copilot, codex, gemini, default: "claude" } } as Partial<Prefs>))
        .map((s) => s.id);
    expect(agents(true, true, false)).toContain("agent-default");
    expect(agents(true, false, true)).toContain("agent-default");
    expect(agents(false, true, true)).toContain("agent-default");
    expect(agents(true, true, true)).toContain("agent-default");
    expect(agents(false, false, true)).not.toContain("agent-default");
    // Gemini counts as one of the two (docs/GEMINI_BACKEND.md D17).
    expect(agents(true, false, false, true)).toContain("agent-default");
    expect(agents(false, false, false, true)).not.toContain("agent-default");
    // An older server says nothing of Codex: two of the other two still count.
    const old = ctx({}, { agents: { claude: true, copilot: true, default: "claude" } } as unknown as Partial<Prefs>);
    expect(visibleSettings(old).map((s) => s.id)).toContain("agent-default");
  });

  test("the four agent switches stand in the registry's order", () => {
    const ids = visibleSettings(ctx()).map((s) => s.id as string).filter((id) => /^agent-(claude|copilot|codex|gemini)$/.test(id));
    expect(ids).toEqual(["agent-claude", "agent-copilot", "agent-codex", "agent-gemini"]);
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

  test("Experimental flags keeps its sections in order, Windows last", () => {
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
    // "sync" also matches the page name (Sharing & sync), so the page's child row follows.
    expect(searchSettings(all, "git sync").map((h) => h.id)).toEqual(["git-sync", "github-link"]);
    expect(searchSettings(all, "git nonsenseword")).toEqual([]);
  });

  test("pointers to other pages are findable", () => {
    // The model pointer leads into the Claude Code group, so it needs ~/.claude.
    const rows = visibleSettings(ctx({ claudeConfig: true }));
    expect(searchSettings(rows, "opus").map((h) => h.id)).toEqual(["claude-model"]);
    expect(searchSettings(all, "opus")).toEqual([]);
  });

  test("the Claude Code group shows only its not-installed row without ~/.claude", () => {
    const ids = (claudeConfig: boolean) =>
      visibleSettings(ctx({ claudeConfig }))
        .filter((e) => e.group === "claude-cli")
        .map((e) => e.id as string);
    expect(ids(false)).toEqual(["claude-cli-missing"]);
    expect(ids(true)).toEqual([]);
  });

  test("Gemini is findable by its own words and Google's", () => {
    for (const q of ["gemini", "google"]) {
      expect(searchSettings(all, q).map((h) => h.id)).toContain("agent-gemini");
    }
    expect(searchSettings(all, "gemini sign").map((h) => h.id)).toEqual(["agent-gemini", "agent-signin"]);
  });

  test("Codex is findable by its own words and its account's", () => {
    for (const q of ["codex", "openai", "chatgpt"]) {
      expect(searchSettings(all, q).map((h) => h.id)).toContain("agent-codex");
    }
    expect(searchSettings(all, "codex sign").map((h) => h.id)).toEqual(["agent-codex", "agent-signin"]);
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

describe("device capability gates", () => {
  const byId = (id: string) => SETTINGS.find((s) => s.id === id)!;

  test("a tagged row reports the server's reason, an untagged or present one null", () => {
    const missing = ctx({}, { unavailable: { index: "Needs pip install 'fused-render[index]'" } });
    expect(unavailableReason(byId("indexing"), missing)).toBe("Needs pip install 'fused-render[index]'");
    expect(unavailableReason(byId("huggingface"), missing)).toBeNull();
    expect(unavailableReason(byId("appearance"), missing)).toBeNull();
    expect(unavailableReason(byId("indexing"), ctx())).toBeNull();
    expect(unavailableReason(byId("indexing"), null)).toBeNull();
  });

  test("every capability row carries the right tag", () => {
    const tags: Record<DeviceFeature, string[]> = {
      index: ["indexing", "ranked-search", "low-impact", "skipped-folders", "rebuild-index", "index-query"],
      hf: ["huggingface"],
      fused: ["fused-account", "workbench-env", "canvases", "canvases-embed-proxy", "share-sheet"],
      update: ["auto-update"],
      native_windows: ["native-windows"],
      terminal: ["notify-terminal"],
    };
    for (const [need, ids] of Object.entries(tags) as [DeviceFeature, string[]][]) for (const id of ids) expect(byId(id).needs).toBe(need);
  });

  test("native-windows is shown, not hidden, when this process has no windows", () => {
    const c = ctx({}, { native_windows: { available: false, enabled: true } });
    expect(visibleSettings(c).some((e) => e.id === "native-windows")).toBe(true);
  });

  test("a disabled row stays findable by search", () => {
    const c = ctx({}, { unavailable: { index: "x" } });
    expect(searchSettings(visibleSettings(c), "index").some((e) => e.id === "indexing")).toBe(true);
  });
});
