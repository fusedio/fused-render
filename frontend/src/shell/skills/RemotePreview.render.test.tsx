// RemotePreview: the repo README as the main content, a meta line instead of a file list.
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";
import { createElement } from "react";
import { installDomShim } from "@platform/lib/testDomShim";
import { formatInstalls, groupRemote, rankRemote } from "./model";

installDomShim();
const { RemotePreview, formatBytes } = await import("./RemotePreview");

function textOf(node: ReactTestInstance | string): string {
  if (typeof node === "string") return node;
  return node.children.map(textOf).join("");
}

const SKILL = { id: "o/r/foo", source: "o/r", skillId: "foo", name: "foo", installs: 5, added: false };
const realFetch = globalThis.fetch;
let reply: Record<string, unknown> = {};

beforeEach(() => {
  globalThis.fetch = ((_u: unknown, _i?: RequestInit) => {
    const text = JSON.stringify(reply);
    return Promise.resolve({ ok: true, status: 200, json: async () => reply, text: async () => text } as unknown as Response);
  }) as typeof fetch;
});
afterEach(() => { globalThis.fetch = realFetch; });

async function mount(): Promise<ReactTestRenderer> {
  let box!: ReactTestRenderer;
  await act(async () => {
    box = create(createElement(RemotePreview, { skill: SKILL, added: false, busy: false, readers: ["claude"], onAdd() {} }));
  });
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  return box;
}

describe("RemotePreview", () => {
  test("shows the README under a label, a meta line, and no file list", async () => {
    reply = { ok: true, text: "skill", readme: "---\nx: 1\n---\n# Repo\nHello", readmeIsSkill: false, fileCount: 358, totalBytes: 2200000 };
    const box = await mount();
    const text = textOf(box.root);
    expect(text).toContain("README · o/r");
    expect(text).toContain("358 files · 2.1 MB");
    const md = box.root.findAll((n) => typeof n.props.text === "string")[0];
    expect(md.props.text).toContain("Hello");
    expect(md.props.text).not.toContain("x: 1");
    expect(box.root.findAll((n) => String(n.props.className ?? "").includes("cc-aside-files"))).toHaveLength(0);
  });

  test("falls back to the SKILL.md label and a singular file count", async () => {
    reply = { ok: true, text: "s", readme: "# S", readmeIsSkill: true, fileCount: 1, totalBytes: 300 };
    const text = textOf((await mount()).root);
    expect(text).toContain("SKILL.md");
    expect(text).not.toContain("README ·");
    expect(text).toContain("1 file · 300 B");
  });
});

describe("RemotePreview too large", () => {
  test("a tooLarge reply shows a muted note, not the error banner", async () => {
    reply = { ok: false, tooLarge: true, error: "This skill is too large to add (over 25 MB or 500 files)." };
    const box = await mount();
    const text = textOf(box.root);
    expect(text).toContain("This skill is too large to add (over 25 MB or 500 files). Open it on GitHub to read it.");
    const add = box.root.findAll((n) => n.props.size === "sm" && typeof n.props.onClick === "function")[0];
    expect(add.props.disabled).toBe(true);
    expect(text).toContain("View on GitHub");
    expect(box.root.findAll((n) => n.props["data-testid"] === "remote-preview-toolarge")).not.toHaveLength(0);
  });
});

describe("helpers", () => {
  test("formatBytes", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(2048)).toBe("2.0 KB");
    expect(formatBytes(2200000)).toBe("2.1 MB");
  });
  test("formatInstalls", () => {
    expect(formatInstalls(0)).toBe("");
    expect(formatInstalls(1)).toBe("1 install");
    expect(formatInstalls(12)).toBe("12 installs");
    expect(formatInstalls(3854)).toBe("3.9k installs");
    expect(formatInstalls(1_200_000)).toBe("1.2M installs");
    expect(formatInstalls(999_999)).toBe("1M installs");
  });
  test("groupRemote sorts by installs and folds a shared skillId", () => {
    const g = groupRemote([
      { skillId: "a", installs: 1, id: "1" }, { skillId: "b", installs: 5, id: "2" }, { skillId: "a", installs: 9, id: "3" },
    ]);
    expect(g.map((x) => [x.top.id, x.rest.map((r) => r.id)])).toEqual([["3", ["1"]], ["2", []]]);
  });
  test("rankRemote orders by relevance tier, then installs", () => {
    const row = (name: string, source: string, installs: number) => ({ name, source, installs, skillId: name, id: name });
    const groups = [
      row("design-mobile", "o/react-kit", 900), row("my-react-thing", "o/x", 10), row("react-best", "o/y", 5),
      row("react", "o/z", 1), row("other", "o/w", 999),
    ].map((r) => ({ top: r, rest: [] }));
    expect(rankRemote(groups, "React").map((g) => g.top.name)).toEqual(["react", "my-react-thing", "react-best", "design-mobile", "other"]);
  });
});
