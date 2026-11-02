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

function fakeFetch(): Promise<Response> {
  const files = Array.from({ length: fileCount }, (_, i) => ({ name: `note-${i}.md`, description: `memory ${i}` }));
  const data = { projects: [{ project: "-p", path: "/work/p", pathConfirmed: true, files }] };
  return Promise.resolve({ ok: true, status: 200, json: async () => data, text: async () => JSON.stringify(data) } as unknown as Response);
}

beforeEach(() => {
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
