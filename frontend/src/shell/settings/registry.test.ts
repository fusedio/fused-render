import { describe, expect, test } from "bun:test";
import type { Prefs } from "@platform/lib/api";
import {
  GROUPS,
  SETTINGS,
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

  test("the four old tab ids still name a group, so old links land", () => {
    for (const tab of ["ai", "indexing", "lan", "account"]) {
      expect(GROUPS.some((g) => g.id === tab)).toBe(true);
    }
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
  test("Fused Bot drops the Render-only groups and rows", () => {
    const bot = visibleSettings(ctx({ bot: true }));
    const groups = visibleGroups(bot).map((g) => g.id);
    expect(groups).not.toContain("apps");
    expect(groups).not.toContain("indexing");
    expect(bot.map((s) => s.id)).not.toContain("canvases");
    expect(bot.map((s) => s.id)).not.toContain("fused-account");
    // Tasks and the monitor exist under Bot too.
    expect(groups).toContain("tasks");
    expect(bot.map((s) => s.id)).toContain("monitor");
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
    expect(secs).toEqual(["Appearance", "Windows", "Keyboard", "Updates"]);
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
    expect(resolveGroup("account", groups)).toBe("account");
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
    expect(searchSettings(all, "opus").map((h) => h.id)).toEqual(["claude-model"]);
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
