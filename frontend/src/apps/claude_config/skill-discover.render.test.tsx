// Discover groups skills by plugin: one section per plugin, a filter that drops
// the sections with nothing to show, and Installed/Added in place of buttons.
import { describe, expect, test } from "bun:test";
import { act, create, type ReactTestInstance } from "react-test-renderer";
import { createElement } from "react";
import { installDomShim } from "@platform/lib/testDomShim";
import type { AvailableSkill } from "./api";

installDomShim();
const { SkillGroupSection, groupSkills } = await import("./skill-discover");

function textOf(node: ReactTestInstance | string): string {
  if (typeof node === "string") return node;
  return node.children.map(textOf).join("");
}

const sk = (over: Partial<AvailableSkill>): AvailableSkill => ({
  slug: "x", name: "x", description: "", plugin: "kit", pluginDescription: "A kit.",
  marketplace: "acme", path: "/p", pluginInstalled: false, added: false, ...over,
});

const ALL = [
  sk({ slug: "brew", name: "Brew", description: "Makes coffee." }),
  sk({ slug: "grind", name: "Grind", description: "Grinds beans.", added: true }),
  sk({ slug: "lint", name: "Lint", description: "Checks code.", plugin: "tools", pluginDescription: "", pluginInstalled: true }),
];

describe("groupSkills", () => {
  test("one group per plugin, carrying the plugin's description", () => {
    const g = groupSkills(ALL, "");
    expect(g.map((x) => [x.plugin, x.skills.length, x.description])).toEqual([
      ["kit", 2, "A kit."],
      ["tools", 1, ""],
    ]);
  });

  test("a filter keeps matching skills and hides a plugin with none", () => {
    const g = groupSkills(ALL, "coffee");
    expect(g.map((x) => x.plugin)).toEqual(["kit"]);
    expect(g[0].skills.map((s) => s.slug)).toEqual(["brew"]);
  });

  test("a plugin-name match shows all its skills", () => {
    const g = groupSkills(ALL, "kit");
    expect(g).toHaveLength(1);
    expect(g[0].skills).toHaveLength(2);
  });
});

describe("a plugin section", () => {
  const mount = (g: ReturnType<typeof groupSkills>[number]) => {
    let box!: ReturnType<typeof create>;
    act(() => {
      box = create(createElement(SkillGroupSection, { group: g, busy: null, onAdd: () => {}, onInstall: () => {} }));
    });
    return box;
  };
  const buttons = (box: ReturnType<typeof create>) =>
    box.root.findAll((n) => n.type === "button").map(textOf);

  test("header names the plugin and marketplace; skills get Add, or muted Added", () => {
    const box = mount(groupSkills(ALL, "")[0]);
    const text = textOf(box.root);
    expect(text).toContain("kit");
    expect(text).toContain("· acme");
    expect(text).toContain("A kit.");
    expect(buttons(box)).toEqual(["Install plugin", "Add"]);
    expect(text).toContain("Added");
    expect(box.root.findAll((n) => n.props["aria-haspopup"] === "menu")).toHaveLength(0);
  });

  test("an installed plugin reads Installed throughout and offers no button", () => {
    const box = mount(groupSkills(ALL, "")[1]);
    expect(buttons(box)).toEqual([]);
    expect(textOf(box.root).match(/Installed/g)).toHaveLength(2);
  });
});
