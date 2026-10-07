// Colour presets: orthogonal to the System/Light/Dark pref. Pins the storage
// contract and that applying a preset only ever touches `data-theme-name`.
import { beforeEach, expect, test } from "bun:test";

import { installDomShim } from "./testDomShim";

installDomShim();

const store = new Map<string, string>();
const attrs = new Map<string, string>();
const g = globalThis as unknown as Record<string, unknown>;
g.localStorage = {
  getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
  setItem: (k: string, v: string) => void store.set(k, v),
  removeItem: (k: string) => void store.delete(k),
};
g.document = {
  documentElement: {
    setAttribute: (k: string, v: string) => void attrs.set(k, v),
    removeAttribute: (k: string) => void attrs.delete(k),
    getAttribute: (k: string) => (attrs.has(k) ? attrs.get(k)! : null),
  },
};

const theme = await import("./theme");

beforeEach(() => {
  store.clear();
  attrs.clear();
});

test("preset list leads with default and has the four ids", () => {
  expect(theme.THEME_PRESETS.map((p) => p.id)).toEqual([
    "default",
    "nord",
    "solarized",
    "high-contrast",
  ]);
  expect(theme.PRESET_KEY).toBe("fused-render:theme-preset");
});

test("load falls back to default for absent or unknown values", () => {
  expect(theme.loadThemePreset()).toBe("default");
  store.set(theme.PRESET_KEY, "bogus");
  expect(theme.loadThemePreset()).toBe("default");
  store.set(theme.PRESET_KEY, "nord");
  expect(theme.loadThemePreset()).toBe("nord");
});

test("setThemePreset persists, applies data-theme-name, leaves data-theme alone", () => {
  attrs.set("data-theme", "light");
  theme.setThemePreset("solarized");
  expect(store.get(theme.PRESET_KEY)).toBe("solarized");
  expect(attrs.get("data-theme-name")).toBe("solarized");
  expect(attrs.get("data-theme")).toBe("light");
  expect(store.has(theme.THEME_KEY)).toBe(false);
});

test("applyPreset(default) clears the attribute", () => {
  theme.applyPreset("nord");
  expect(attrs.get("data-theme-name")).toBe("nord");
  theme.applyPreset("default");
  expect(attrs.has("data-theme-name")).toBe(false);
});

test("a throwing localStorage degrades to default and does not throw on write", () => {
  g.localStorage = {
    getItem: () => {
      throw new Error("blocked");
    },
    setItem: () => {
      throw new Error("blocked");
    },
  };
  expect(theme.loadThemePreset()).toBe("default");
  expect(() => theme.setThemePreset("nord")).not.toThrow();
  expect(attrs.get("data-theme-name")).toBe("nord");
});
