import { describe, expect, test } from "bun:test";
import type { PrefEntry } from "@apps/claude_config/api";
import { CLAUDE_PREF_PREFIX, claudePrefEntries, isClaudePref } from "./claude";
import { searchSettings, tokenize } from "./search";

const SCHEMA: PrefEntry[] = [
  { key: "model", label: "Model", group: "Model & reasoning", control: "select", options: ["opus", "sonnet"], doc: "Override the default model." },
  { key: "effortLevel", label: "Effort level", group: "Model & reasoning", control: "select", options: ["low", "high"], doc: null },
  { key: "permissions.defaultMode", label: "Default permission mode", group: "Permissions", control: "select", options: ["default", "plan"] },
];

describe("the settings.json rows", () => {
  test("one row per catalog entry, in catalog order, sectioned by the catalog's group", () => {
    const rows = claudePrefEntries(SCHEMA);
    expect(rows.map((r) => r.id)).toEqual([
      "claude-pref:model",
      "claude-pref:effortLevel",
      "claude-pref:permissions.defaultMode",
    ]);
    expect(rows.map((r) => r.section)).toEqual(["Model & reasoning", "Model & reasoning", "Permissions"]);
    for (const r of rows) {
      expect(r.group).toBe("claude");
      expect(isClaudePref(r)).toBe(true);
      expect(r.id.startsWith(CLAUDE_PREF_PREFIX)).toBe(true);
    }
    expect(rows[0].prefKey).toBe("model");
    expect(rows[1].description).toBe("");
  });

  test("a search finds them by key, label, doc, option and group", () => {
    const rows = claudePrefEntries(SCHEMA);
    const find = (q: string) => searchSettings(rows, tokenize(q)).map((r) => r.prefKey);
    expect(find("effortlevel")).toEqual(["effortLevel"]);
    expect(find("override")).toEqual(["model"]);
    expect(find("sonnet")).toEqual(["model"]);
    expect(find("permissions")).toEqual(["permissions.defaultMode"]);
    expect(find("reasoning")).toEqual(["model", "effortLevel"]);
  });

  test("a registry row is not one of them", () => {
    expect(isClaudePref({ id: "claude-catalog", group: "claude", section: "", label: "", description: "", keywords: "" })).toBe(false);
  });
});
