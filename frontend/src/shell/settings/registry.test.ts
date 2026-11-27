import { describe, expect, test } from "bun:test";
import type { Prefs } from "@platform/lib/api";
import {
  GROUPS,
  NAV_SECTIONS,
  SETTINGS,
  navSections,
  searchableSettings,
  legacyTabTarget,
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

  test("a group split into sections draws none of one row", () => {
    // Everything a Mac with ~/.claude can show, every parent switch off.
    const rows = visibleSettings(ctx({ claudeConfig: true }, { chromium_windows: { available: true } } as Partial<Prefs>));
    for (const g of GROUPS) {
      const sections = sectionsOf(rows, g.id);
      if (sections.length < 2) continue;
      for (const sec of sections) {
        // Integrations > Fused Workbench is its switch alone while that switch
        // is off (the environment rows wait for it): the one accepted exception.
        if (g.id === "integrations" && sec.section === "Fused Workbench") continue;
        expect([g.id, sec.section, sec.entries.length >= 2]).toEqual([g.id, sec.section, true]);
      }
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
  test("child rows are hidden until their parent is on", () => {
    const ids = (prefs: Partial<Prefs>) => visibleSettings(ctx({}, prefs)).map((e) => e.id as string);
    const children = ["call-params", "call-retention"];
    for (const c of children) expect(ids({})).not.toContain(c);
    const on = ids({ calls: { effective_enabled: true } } as unknown as Partial<Prefs>);
    for (const c of children) expect(on).toContain(c);
    // …but search still finds them.
    const pool = searchableSettings(ctx());
    expect(searchSettings(pool, "retention").map((e) => e.id)).toEqual(["call-retention"]);
  });

  test("the rail's own switches are top-level rows: the rail, favourites and the shared header are always on", () => {
    const ids = visibleSettings(ctx({}, { canvases: { enabled: false } } as Partial<Prefs>)).map((s) => s.id as string);
    for (const gone of ["canvases-rail-peek", "tasks-favourites", "tasks-unified-head"]) expect(ids).not.toContain(gone);
  });

  test("Rail extras is one top-level switch in place of three, and task icons are always on", () => {
    const all = SETTINGS.map((s) => s.id as string);
    for (const gone of ["tasks-icons", "rail-mode-doors", "rail-expand", "rail-free-pins"]) expect(all).not.toContain(gone);
    const row = SETTINGS.find((s) => s.id === "rail-extras")!;
    expect(row.label).toBe("Rail extras");
    expect("child" in row).toBe(false);
    expect("parent" in row).toBe(false);
    expect(visibleSettings(ctx()).map((s) => s.id as string)).toContain("rail-extras");
  });

  test("the nav has no Claude Code section; it is the Claude Code page", () => {
    const nav = navSections(visibleGroups(visibleSettings(ctx({ claudeConfig: true })))).map((s) => s.section.id);
    expect(nav).toEqual(["app", "agents", "apps", "developer"]);
  });

  test("Sharing & sync is one card, Remix its only Community row (the store and the loop are always on)", () => {
    const secs = sectionsOf(visibleSettings(ctx()), "apps");
    expect(secs.map((s) => s.section)).toEqual(["Sharing & sync"]);
    expect(secs[0].entries.map((e) => e.id)).toContain("contrib-remix");
    expect(visibleSettings(ctx()).map((s) => s.id as string)).not.toContain("marketplace");
    expect(visibleSettings(ctx()).map((s) => s.id as string)).not.toContain("contrib");
  });

  test("the launcher rows speak of apps", () => {
    expect(visibleSettings(ctx()).find((s) => s.id === "launcher-row")!.label).toBe("Open the Nth app");
  });

  test("capabilities the server says are missing are not drawn", () => {
    const ids = visibleSettings(ctx({}, { launcher: undefined } as Partial<Prefs>)).map((s) => s.id);
    expect(ids).not.toContain("launcher-hotkey");
  });


  test("the embed proxy switch appears only while Workbench and On-prem are on", () => {
    const ids = (enabled: boolean, envs: string[]) =>
      visibleSettings(
        ctx({}, { canvases: { enabled }, workbench: { env: "prod", locked: false, choices: [], enabled: envs } } as Partial<Prefs>),
      ).map((s) => s.id);
    expect(visibleSettings(ctx()).map((s) => s.id)).not.toContain("canvases-embed-proxy");
    expect(ids(false, ["prod", "custom"])).not.toContain("canvases-embed-proxy");
    expect(ids(true, ["prod"])).not.toContain("canvases-embed-proxy");
    expect(ids(true, ["prod", "custom"])).toContain("canvases-embed-proxy");
  });

  test("the environment rows wait for Fused Workbench, and Unstable has none", () => {
    const ids = (enabled: boolean) =>
      visibleSettings(ctx({}, { canvases: { enabled } } as Partial<Prefs>)).map((s) => s.id);
    expect(ids(false)).toContain("canvases");
    expect(ids(false)).not.toContain("workbench-prod");
    expect(ids(true)).toEqual(expect.arrayContaining(["canvases", "workbench-prod", "workbench-custom"]));
    expect(SETTINGS.map((s) => s.id as string)).not.toContain("workbench-unstable");
  });

  test("the default-agent picker appears only with two or more agents on", () => {
    expect(visibleSettings(ctx()).map((s) => s.id)).not.toContain("agent-default");
    const agents = (claude: boolean, copilot: boolean, codex: boolean, gemini = false, opencode = false) =>
      visibleSettings(ctx({}, { agents: { claude, copilot, codex, gemini, opencode, default: "claude" } } as Partial<Prefs>))
        .map((s) => s.id);
    expect(agents(true, true, false)).toContain("agent-default");
    expect(agents(true, false, true)).toContain("agent-default");
    expect(agents(false, true, true)).toContain("agent-default");
    expect(agents(true, true, true)).toContain("agent-default");
    expect(agents(false, false, true)).not.toContain("agent-default");
    // Gemini counts as one of the two (docs/GEMINI_BACKEND.md D17).
    expect(agents(true, false, false, true)).toContain("agent-default");
    expect(agents(false, false, false, true)).not.toContain("agent-default");
    // OpenCode counts too (docs/OPENCODE_BACKEND.md D17).
    expect(agents(true, false, false, false, true)).toContain("agent-default");
    expect(agents(false, false, false, false, true)).not.toContain("agent-default");
    // An older server says nothing of Codex: two of the other two still count.
    const old = ctx({}, { agents: { claude: true, copilot: true, default: "claude" } } as unknown as Partial<Prefs>);
    expect(visibleSettings(old).map((s) => s.id)).toContain("agent-default");
  });

  test("the five agent switches stand in the registry's order", () => {
    const ids = visibleSettings(ctx()).map((s) => s.id as string).filter((id) => /^agent-(claude|copilot|codex|gemini|opencode)$/.test(id));
    expect(ids).toEqual(["agent-claude", "agent-copilot", "agent-codex", "agent-gemini", "agent-opencode"]);
  });

  test("sections come out in first-appearance order", () => {
    const secs = sectionsOf(visibleSettings(ctx()), "general").map((s) => s.section);
    expect(secs).toEqual(["Appearance", "Windows & updates", "Keyboard", "Tools"]);
  });

  test("the Chromium engine row is drawn only where the server says it is available", () => {
    const off = ctx({}, { chromium_windows: { available: false, enabled: false } } as unknown as Partial<Prefs>);
    expect(visibleSettings(off).map((s) => s.id)).not.toContain("chromium-windows");
    expect(visibleSettings(ctx()).map((s) => s.id)).not.toContain("chromium-windows");
    const on = ctx({}, { chromium_windows: { available: true, enabled: false } } as unknown as Partial<Prefs>);
    expect(visibleSettings(on).map((s) => s.id)).toContain("chromium-windows");
  });

  test("Experimental flags: one card, Rail extras first", () => {
    const on = ctx({}, { chromium_windows: { available: true, enabled: false } } as unknown as Partial<Prefs>);
    const secs = sectionsOf(visibleSettings(on), "features");
    expect(secs.map((s) => s.section)).toEqual(["Other"]);
    expect(secs[0].entries.map((e) => e.id)).toEqual(["rail-extras", "left-rail-peek", "experts", "chromium-windows"]);
    // Without the capability the Chromium row is simply missing.
    expect(sectionsOf(visibleSettings(ctx()), "features").map((s) => s.section)).toEqual(["Other"]);
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
  test("the old Accounts, Fused Workbench and Connections tabs read as Integrations", () => {
    for (const tab of ["account", "workbench", "connections"]) expect(resolveGroup(tab, groups)).toBe("integrations");
    expect(legacyTabTarget("workbench")).toBe("canvases");
    expect(legacyTabTarget("connections")).toBe("connections");
    expect(legacyTabTarget("account")).toBeNull();
    expect(legacyTabTarget("indexing")).toBeNull();
  });
  test("a tab with no visible group falls back", () => {
    expect(resolveGroup("indexing", GROUPS.filter((g) => g.id !== "indexing"))).toBe("general");
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
    // A word the reader can see in the row is not reported as a synonym…
    expect(matchedVia(hit, ["read"])).toEqual([]);
    // …but one from the description is: it sits behind the ⓘ.
    expect(matchedVia(hit, ["aloud"])).toEqual(["aloud"]);
    expect(searchSettings(all, "token").map((h) => h.id)).toEqual(["huggingface"]);

    const dark = searchSettings(all, "dark").map((h) => h.id);
    expect(dark).toContain("appearance");
  });

  test("every word must match", () => {
    // "sync" also matches the page name (Sharing & sync).
    expect(searchSettings(all, "git sync").map((h) => h.id)).toEqual(["git-sync"]);
    expect(searchSettings(all, "git nonsenseword")).toEqual([]);
  });

  test("the Set elsewhere pointer rows are gone", () => {
    const ids = SETTINGS.map((s) => s.id as string);
    for (const gone of ["claude-model", "engines", "agent-signin"]) expect(ids).not.toContain(gone);
    expect(SETTINGS.some((s) => s.section === "Set elsewhere")).toBe(false);
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
    expect(searchSettings(all, "gemini sign").map((h) => h.id)).toEqual(["agent-gemini"]);
  });

  test("OpenCode is findable by its own words and by 'local model'", () => {
    for (const q of ["opencode", "local model"]) {
      expect(searchSettings(all, q).map((h) => h.id)).toContain("agent-opencode");
    }
    expect(searchSettings(all, "opencode sign").map((h) => h.id)).toEqual(["agent-opencode"]);
  });

  test("Codex is findable by its own words and its account's", () => {
    for (const q of ["codex", "openai", "chatgpt"]) {
      expect(searchSettings(all, q).map((h) => h.id)).toContain("agent-codex");
    }
    expect(searchSettings(all, "codex sign").map((h) => h.id)).toEqual(["agent-codex"]);
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
      fused: ["workbench-prod", "workbench-custom", "canvases", "canvases-embed-proxy", "share-sheet"],
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
