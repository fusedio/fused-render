// A project's memory files cap at 6 rows with a "Show N more" row; the group
// head still counts every file.
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";
import { createElement } from "react";
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();
const { default: MemorySection } = await import("./sections/MemorySection");
const { rankPaths, deriveHome } = await import("./ProjectPathSearch");

function textOf(node: ReactTestInstance | string): string {
  if (typeof node === "string") return node;
  return node.children.map(textOf).join("");
}

const realFetch = globalThis.fetch;
let fileCount = 8;
let multi = false;

function fakeFetch(): Promise<Response> {
  const files = Array.from({ length: fileCount }, (_, i) => ({ name: `note-${i}.md`, description: `memory ${i}` }));
  const data = multi
    ? {
        projects: [
          { project: "-z", path: "/work/zeta", pathConfirmed: true, files: [{ name: "zfile.md", description: null }] },
          { project: "-gone-x", path: null, pathConfirmed: false, pathGuess: "/gone/Alpha", files: [{ name: "afile.md", description: null }] },
        ],
      }
    : { projects: [{ project: "-p", path: "/work/p", pathConfirmed: true, files }] };
  return Promise.resolve({ ok: true, status: 200, json: async () => data, text: async () => JSON.stringify(data) } as unknown as Response);
}

beforeEach(() => {
  multi = false;
  globalThis.fetch = fakeFetch as unknown as typeof fetch;
});
afterEach(() => {
  globalThis.fetch = realFetch;
});

async function mount(filter = "") {
  let box!: ReactTestRenderer;
  await act(async () => {
    box = create(createElement(MemorySection, { filter, title: "Memory", blurb: "b" }));
  });
  for (let i = 0; i < 3; i++) await act(async () => { await new Promise((r) => setTimeout(r, 5)); });
  return box;
}
const rows = (box: ReactTestRenderer) => textOf(box.root).match(/note-\d+\.md/g)?.length ?? 0;
const more = (box: ReactTestRenderer) => box.root.findAllByProps({ className: "cc-showmore" }).filter((n) => typeof n.type === "string");

describe("MemorySection show more", () => {
  test("8 files show 6, Show 2 more expands, Show less collapses", async () => {
    fileCount = 8;
    const box = await mount();
    expect(rows(box)).toBe(6);
    expect(textOf(more(box)[0])).toBe("Show 2 more");
    await act(async () => { more(box)[0].props.onClick(); });
    expect(rows(box)).toBe(8);
    expect(textOf(more(box)[0])).toBe("Show less");
    await act(async () => { more(box)[0].props.onClick(); });
    expect(rows(box)).toBe(6);
  });

  test("6 files show all with no button", async () => {
    fileCount = 6;
    const box = await mount();
    expect(rows(box)).toBe(6);
    expect(more(box)).toHaveLength(0);
  });

  test("changing the filter collapses an expanded list", async () => {
    fileCount = 8;
    const box = await mount();
    await act(async () => { more(box)[0].props.onClick(); });
    expect(rows(box)).toBe(8);
    await act(async () => { box.update(createElement(MemorySection, { filter: "note", title: "Memory", blurb: "b" })); });
    expect(rows(box)).toBe(6);
  });
});

describe("rankPaths", () => {
  const items = ["/w/zeta-app", "/w/app/other", "/w/myapp", "/w/app"].map((title) => ({ title }));
  test("last-segment prefix, then contains, then segment prefix, then elsewhere", () => {
    expect(rankPaths(items, "app").map((i) => i.title)).toEqual(["/w/app", "/w/myapp", "/w/zeta-app", "/w/app/other"]);
  });
  test("every token must match, case-insensitively; ~ form also matches", () => {
    expect(rankPaths(items, "W ZETA").map((i) => i.title)).toEqual(["/w/zeta-app"]);
    expect(rankPaths(items, "nope")).toEqual([]);
    expect(rankPaths([{ title: "/Users/me/x" }], "~/x", "/Users/me")).toHaveLength(1);
  });
  test("deriveHome from confirmed paths", () => {
    expect(deriveHome(["/Users/me/a", "/Users/me/b", "/opt/x"])).toBe("/Users/me");
    expect(deriveHome(["/opt/x"])).toBe("");
  });
});

describe("MemorySection project picker", () => {
  const input = (box: ReactTestRenderer) => box.root.findByProps({ role: "combobox" });
  const options = (box: ReactTestRenderer) => box.root.findAll((n) => n.props.role === "option" && typeof n.type === "string");
  const type = async (box: ReactTestRenderer, v: string) => {
    await act(async () => { input(box).props.onFocus(); });
    await act(async () => { input(box).props.onChange({ target: { value: v } }); });
  };
  const key = async (box: ReactTestRenderer, k: string) => {
    await act(async () => { input(box).props.onKeyDown({ key: k, preventDefault() {} }); });
  };

  test("focus lists every project with counts, sorted by title", async () => {
    multi = true;
    const box = await mount();
    await act(async () => { input(box).props.onFocus(); });
    expect(options(box).map(textOf)).toEqual(["/gone/Alphafolder not found1", "/work/zeta1"]);
  });

  test("typing narrows options but does not filter the groups", async () => {
    multi = true;
    const box = await mount();
    await type(box, "zeta");
    expect(options(box)).toHaveLength(1);
    expect(textOf(box.root)).toContain("afile.md");
  });

  test("ArrowDown+Enter selects and filters groups; clear restores all", async () => {
    multi = true;
    const box = await mount();
    await type(box, "");
    await key(box, "ArrowDown");
    await key(box, "Enter");
    expect(textOf(box.root)).toContain("afile.md");
    expect(textOf(box.root)).not.toContain("zfile.md");
    expect(input(box).props.value).toBe("/gone/Alpha");
    const clear = box.root.findByProps({ "aria-label": "Clear project filter" });
    await act(async () => { clear.props.onClick(); });
    expect(textOf(box.root)).toContain("zfile.md");
    expect(textOf(box.root)).toContain("afile.md");
  });

  test("Enter with one match and no active option selects it", async () => {
    multi = true;
    const box = await mount();
    await type(box, "zeta");
    await key(box, "Enter");
    expect(textOf(box.root)).not.toContain("afile.md");
  });

  test("Escape closes the list", async () => {
    multi = true;
    const box = await mount();
    await type(box, "z");
    expect(options(box).length).toBeGreaterThan(0);
    await key(box, "Escape");
    expect(options(box)).toHaveLength(0);
  });

  test("a guessed group shows the guess and a folder-not-found tag", async () => {
    multi = true;
    const box = await mount();
    const text = textOf(box.root);
    expect(text).toContain("/gone/Alpha");
    expect(text).toContain("folder not found");
  });
});
