import { expect, test } from "bun:test";
import { installDomShim } from "@platform/lib/testDomShim";
import type { Plugin } from "../api";

installDomShim();
const { selectInstalled } = await import("./PluginsSection");

const plugin = (name: string, over: Partial<Plugin> = {}): Plugin =>
  ({ id: `${name}@m`, name, marketplace: "m", enabled: true, installed: true, description: "", ...over }) as Plugin;

test("a plugin that is only enabled in settings is not listed as installed", () => {
  const list = [plugin("figma"), plugin("ghost", { installed: false })];
  expect(selectInstalled(list, "").map((p) => p.name)).toEqual(["figma"]);
});

test("the filter is trimmed before matching", () => {
  const list = [plugin("figma"), plugin("other")];
  expect(selectInstalled(list, "  figma ").map((p) => p.name)).toEqual(["figma"]);
});
