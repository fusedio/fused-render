// The Plugins rows: a version is shown only when it is one (a commit hash in
// the row would read `v<hash>`), the row has ONE overflow button, and the
// enable switch is the last thing on the line.
import { describe, expect, test } from "bun:test";
import { act, create, type ReactTestInstance } from "react-test-renderer";
import { createElement } from "react";
import { installDomShim } from "@platform/lib/testDomShim";
import type { AvailablePlugin, Plugin } from "./api";

installDomShim();
const { InstalledPluginRow, DiscoverPluginRow, versionLabel } = await import("./plugin-row");

function textOf(node: ReactTestInstance | string): string {
  if (typeof node === "string") return node;
  return node.children.map(textOf).join("");
}

const plugin = (over: Partial<Plugin> = {}): Plugin => ({
  id: "context7@claude-plugins-official",
  name: "context7",
  marketplace: "claude-plugins-official",
  version: "1.2.3",
  enabled: true,
  installed: true,
  shareCommand: "claude plugin install context7@claude-plugins-official",
  description: "Up-to-date docs for any library.",
  ...over,
} as Plugin);

const noop = () => {};
function mountInstalled(p: Plugin) {
  let box!: ReturnType<typeof create>;
  act(() => {
    box = create(
      createElement(InstalledPluginRow, {
        plugin: p,
        enabled: p.enabled,
        busy: false,
        onToggle: noop,
        onUpdate: noop,
        onUninstall: noop,
        onCopy: noop,
      }),
    );
  });
  return box;
}

describe("versionLabel", () => {
  test("a semver is v-prefixed in the row and the details", () => {
    expect(versionLabel("1.2.3")).toEqual({ row: "v1.2.3", detail: "v1.2.3" });
    expect(versionLabel("0.4.0-beta.1")).toEqual({ row: "v0.4.0-beta.1", detail: "v0.4.0-beta.1" });
  });
  test("a commit hash is silent in the row and `commit <7>` in the details", () => {
    const hash = "ed2e105b6e4c9f0a7d3b1c8e5f2a4d6b8c0e1f3a";
    expect(versionLabel(hash)).toEqual({ row: "", detail: "commit ed2e105" });
    expect(versionLabel("ed2e105")).toEqual({ row: "", detail: "commit ed2e105" });
  });
  test("nothing, or something unrecognised, never gains a v", () => {
    expect(versionLabel("")).toEqual({ row: "", detail: "" });
    expect(versionLabel("unknown")).toEqual({ row: "", detail: "unknown" });
  });
});

describe("an installed plugin row", () => {
  test("shows a semver but never `v<hash>`", () => {
    expect(textOf(mountInstalled(plugin()).root)).toContain("v1.2.3");
    const hashed = mountInstalled(plugin({ version: "ed2e105b6e4c9f0a7d3b1c8e5f2a4d6b8c0e1f3a" }));
    const text = textOf(hashed.root);
    expect(text).not.toContain("ed2e105");
    expect(text).not.toMatch(/\bv[0-9a-f]{7}/);
  });

  test("names the plugin with its description and a muted marketplace, not the id", () => {
    const text = textOf(mountInstalled(plugin()).root);
    expect(text).toContain("context7");
    expect(text).toContain("Up-to-date docs for any library.");
    expect(text).toContain("claude-plugins-official");
    expect(text).not.toContain("context7@claude-plugins-official");
  });

  test("has exactly one overflow button and no icon-button row", () => {
    const box = mountInstalled(plugin());
    const menus = box.root.findAll((n) => n.type === "button" && n.props["aria-haspopup"] === "menu");
    expect(menus).toHaveLength(1);
    expect(box.root.findAll((n) => n.type === "button" && /^(Update|Uninstall|Copy)/.test(n.props["aria-label"] ?? ""))).toHaveLength(0);
  });

  test("the enable switch is the last control on the line", () => {
    const box = mountInstalled(plugin());
    const line = box.root.findByProps({ className: "cc-lrow-line" });
    const kids = line.children.filter((c): c is ReactTestInstance => typeof c !== "string");
    const last = kids[kids.length - 1];
    expect(last.props.className).toBe("cc-lrow-end");
    expect(last.findAll((n) => n.type === "input" && n.props.type === "checkbox")).toHaveLength(1);
  });
});

describe("a discover row", () => {
  const avail = (over: Partial<AvailablePlugin> = {}): AvailablePlugin => ({
    id: "a@m", name: "a", marketplace: "m", description: "Does a thing.", version: "", author: "", category: "",
    keywords: [], installed: false, ...over,
  } as AvailablePlugin);
  const mount = (p: AvailablePlugin) => {
    let box!: ReturnType<typeof create>;
    act(() => {
      box = create(createElement(DiscoverPluginRow, { plugin: p, busy: false, onInstall: noop, onCopy: noop }));
    });
    return box;
  };

  test("ends in an Install button, or muted Installed text", () => {
    const fresh = mount(avail());
    expect(fresh.root.findAll((n) => n.type === "button" && textOf(n) === "Install")).toHaveLength(1);
    const have = mount(avail({ installed: true }));
    expect(have.root.findAll((n) => n.type === "button" && textOf(n) === "Install")).toHaveLength(0);
    expect(textOf(have.root)).toContain("Installed");
  });
});

describe("a row's meta", () => {
  test("is ONE muted string: marketplace, then the version when it is a release", () => {
    const box = mountInstalled(plugin());
    const metas = box.root.findAll((n) => n.type === "span" && String(n.props.className ?? "").includes("cc-lrow-meta"));
    expect(metas).toHaveLength(1);
    expect(textOf(metas[0])).toBe("claude-plugins-official · v1.2.3");
    const hashed = mountInstalled(plugin({ version: "ed2e105b6e4c9f0a7d3b1c8e5f2a4d6b8c0e1f3a" }));
    const m = hashed.root.findAll((n) => n.type === "span" && String(n.props.className ?? "").includes("cc-lrow-meta"));
    expect(textOf(m[0])).toBe("claude-plugins-official");
  });
});
