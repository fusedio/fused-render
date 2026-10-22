// A skill row names the CLIs that read the skill, but only when there is a
// choice to tell apart: with one CLI on, every skill is read by it and a badge
// on every row is noise.
import { describe, expect, test } from "bun:test";
import { act, create, type ReactTestInstance } from "react-test-renderer";
import { createElement } from "react";
import { installDomShim } from "@platform/lib/testDomShim";
import type { Skill } from "./api";

installDomShim();
const { SkillRow } = await import("./skill-row");
const { visibleReaders } = await import("./readers");

function textOf(node: ReactTestInstance | string): string {
  if (typeof node === "string") return node;
  return node.children.map(textOf).join("");
}

const skill = (over: Partial<Skill> = {}): Skill =>
  ({
    slug: "review", name: "review", description: "Reviews a diff.", source: "user", linked: true,
    path: "/h/.agents/skills/review", location: "agents", paths: ["/h/.agents/skills/review"],
    readers: ["claude", "copilot", "codex"], ...over,
  } as Skill);

function mount(s: Skill, on: ("claude" | "copilot" | "codex")[]) {
  let box!: ReturnType<typeof create>;
  act(() => {
    box = create(createElement(SkillRow, { skill: s, enabledClis: on, onOpen: () => {} }));
  });
  return box;
}

describe("visibleReaders", () => {
  test("shows only the CLIs that are on, and nothing when one is", () => {
    expect(visibleReaders(["claude", "copilot", "codex"], ["claude", "codex"])).toEqual(["claude", "codex"]);
    expect(visibleReaders(["claude", "copilot"], ["claude"])).toEqual([]);
    expect(visibleReaders(["claude"], ["claude", "copilot"])).toEqual(["claude"]);
  });
});

describe("a skill row", () => {
  test("badges the readers when more than one CLI is on", () => {
    const text = textOf(mount(skill(), ["claude", "copilot", "codex"]).root);
    expect(text).toContain("Claude");
    expect(text).toContain("Copilot");
    expect(text).toContain("Codex");
  });

  test("a CLI that is off gets no badge", () => {
    const text = textOf(mount(skill(), ["claude", "codex"]).root);
    expect(text).toContain("Codex");
    expect(text).not.toContain("Copilot");
  });

  test("no badges when only one CLI is on", () => {
    const text = textOf(mount(skill(), ["claude"]).root);
    expect(text).toContain("review");
    expect(text).toContain("Reviews a diff.");
    for (const label of ["Claude", "Copilot", "Codex"]) expect(text).not.toContain(label);
  });
});

describe("a skill row's meta", () => {
  test("names the plugin as muted text on the right, not as a pill", () => {
    const box = mount(skill({ source: "plugin", plugin: "kit" }), ["claude"]);
    const metas = box.root.findAll((n) => n.type === "span" && String(n.props.className ?? "").includes("cc-lrow-meta"));
    expect(metas.map(textOf)).toEqual(["kit"]);
    expect(box.root.findAll((n) => String(n.props.className ?? "").includes("cc-pill"))).toHaveLength(0);
  });
});
