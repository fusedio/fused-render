// A skill's page opens on a rendered preview with the frontmatter block
// stripped, says nothing about linking, and a plugin's skill cannot be edited.
import { describe, expect, mock, test } from "bun:test";
import { act, create, type ReactTestInstance } from "react-test-renderer";
import { createElement } from "react";
import { installDomShim } from "@platform/lib/testDomShim";
import * as realApi from "./api";
import type { Skill } from "./api";

installDomShim();

const FILE = "---\nname: review\ndescription: Reviews a diff.\n---\n# Review\n\nBody text.\n";
mock.module("./api", () => ({
  ...realApi,
  skills: {
    ...realApi.skills,
    read: async () => ({ ok: true, text: FILE, files: ["SKILL.md", "refs/a.md"] }),
  },
}));
const { SkillDetail, stripFrontmatter, tildify } = await import("./skill-detail");

function textOf(node: ReactTestInstance | string): string {
  if (typeof node === "string") return node;
  return node.children.map(textOf).join("");
}

const skill = (over: Partial<Skill> = {}): Skill =>
  ({
    slug: "review", name: "review", description: "Reviews a diff.", source: "user", linked: false,
    path: "/home/me/.agents/skills/review", location: "agents", paths: ["/home/me/.agents/skills/review"],
    readers: ["claude", "codex"], ...over,
  } as Skill);

async function mount(s: Skill) {
  let box!: ReturnType<typeof create>;
  await act(async () => {
    box = create(
      createElement(SkillDetail, {
        skill: s, enabledClis: ["claude", "codex"], onBack: () => {}, onChanged: () => {}, onDeleted: () => {},
      }),
    );
  });
  await act(async () => {});
  return box;
}

describe("stripFrontmatter / tildify", () => {
  test("drops only the leading --- block", () => {
    expect(stripFrontmatter(FILE)).toBe("# Review\n\nBody text.\n");
    expect(stripFrontmatter("# No front\n---\nx\n---\n")).toBe("# No front\n---\nx\n---\n");
  });
  test("shortens the home directory", () => {
    expect(tildify("/home/me/.claude/skills/a")).toBe("~/.claude/skills/a");
    expect(tildify("/Users/me/x")).toBe("~/x");
    expect(tildify("/opt/x")).toBe("/opt/x");
  });
});

describe("a skill's page", () => {
  test("never says linked, shows its location and readers, and lists files beside the pane", async () => {
    const box = await mount(skill());
    const text = textOf(box.root);
    expect(text).not.toMatch(/linked/i);
    expect(text).toContain("~/.agents/skills/review");
    expect(text).toContain("Claude");
    expect(text).toContain("Codex");
    expect(box.root.findAllByProps({ className: "cc-skill-filelist" })).toHaveLength(1);
    expect(box.root.findAll((n) => n.type === "button" && textOf(n) === "refs/a.md")).toHaveLength(1);
  });

  test("opens on the preview with the frontmatter stripped; Save waits for an edit", async () => {
    const box = await mount(skill());
    const preview = box.root.findByProps({ className: "cc-md" });
    const html = JSON.stringify(preview.findAll((n) => typeof n.props.dangerouslySetInnerHTML === "object").map((n) => n.props.dangerouslySetInnerHTML));
    expect(html).toContain("Body text.");
    expect(html).not.toContain("description: Reviews");
    expect(box.root.findAll((n) => n.type === "textarea")).toHaveLength(0);
    const save = box.root.findAll((n) => n.type === "button" && textOf(n) === "Save")[0];
    expect(save.props.disabled).toBe(true);
    expect(save.props.className).toBe("btn btn-primary");

    const edit = box.root.findAll((n) => n.type === "button" && textOf(n) === "Edit")[0];
    await act(async () => edit.props.onClick());
    expect(box.root.findAll((n) => n.type === "textarea")).toHaveLength(1);
    expect(box.root.findAll((n) => n.type === "textarea")[0].props.value).toBe(FILE);
  });

  test("a plugin's skill says where it is from, previews, and has no Edit, Save or Delete", async () => {
    const box = await mount(skill({ source: "plugin", plugin: "kit", path: "/p/kit/skills/review" }));
    const text = textOf(box.root);
    expect(text).toContain("from kit · updates to the plugin replace it");
    expect(box.root.findAllByProps({ className: "cc-md" })).toHaveLength(1);
    const labels = box.root.findAll((n) => n.type === "button").map(textOf);
    for (const gone of ["Edit", "Save", "Delete"]) expect(labels).not.toContain(gone);
    expect(labels).toContain("Reveal");
  });
});
