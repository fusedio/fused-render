// A project's memory files cap at 6 rows with a "Show N more" row; the group
// head still counts every file.
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";
import { createElement } from "react";
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();
const { default: MemorySection } = await import("./sections/MemorySection");

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

describe("MemorySection project picker", () => {
  const select = (box: ReactTestRenderer) => box.root.findByType("select");
  test("lists All plus each project, sorted by title", async () => {
    multi = true;
    const box = await mount();
    const opts = select(box).findAllByType("option").map(textOf);
    expect(opts).toEqual(["All projects (2 files)", "/gone/Alpha (1)", "/work/zeta (1)"]);
  });

  test("choosing a project hides the other groups", async () => {
    multi = true;
    const box = await mount();
    expect(textOf(box.root)).toContain("zfile.md");
    expect(textOf(box.root)).toContain("afile.md");
    await act(async () => { select(box).props.onChange({ target: { value: "-z" } }); });
    expect(textOf(box.root)).toContain("zfile.md");
    expect(textOf(box.root)).not.toContain("afile.md");
  });

  test("a guessed group shows the guess and a folder-not-found tag", async () => {
    multi = true;
    const box = await mount();
    const text = textOf(box.root);
    expect(text).toContain("/gone/Alpha");
    expect(text).toContain("folder not found");
  });
});
