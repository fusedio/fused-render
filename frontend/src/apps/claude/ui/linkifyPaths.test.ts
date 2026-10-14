import { expect, test } from "bun:test";

import { linkifyEnabled, linkifyPaths, type LinkTarget } from "./linkifyPaths";

function code(text: string, inPre = false) {
  const classes = new Set<string>();
  const attrs: Record<string, string> = {};
  const el: LinkTarget & { classes: Set<string>; attrs: Record<string, string> } = {
    textContent: text,
    closest: (sel: string) => (sel === "pre" && inPre ? {} : null),
    classList: { add: (c: string) => void classes.add(c) },
    setAttribute: (k: string, v: string) => void (attrs[k] = v),
    classes,
    attrs,
  };
  return el;
}

const rootOf = (els: LinkTarget[]) => ({ querySelectorAll: () => els });

test("an existing path gets the link class, role, tabindex and resolved data-path", async () => {
  const el = code("src/a.ts:12");
  const asked: string[] = [];
  const n = await linkifyPaths(rootOf([el]), {
    cwd: "/w",
    stat: async (p) => (asked.push(p), true),
  });
  expect(n).toBe(1);
  expect(asked).toEqual(["/w/src/a.ts"]);
  expect(el.classes.has("c-path-link")).toBe(true);
  expect(el.attrs.role).toBe("button");
  expect(el.attrs.tabindex).toBe("0");
  expect(el.attrs["data-path"]).toBe("/w/src/a.ts");
});

test("a path that does not exist, plain words, and code inside pre are left alone", async () => {
  const gone = code("src/missing.ts");
  const word = code("foo");
  const fenced = code("src/a.ts", true);
  const stat = async (p: string) => p !== "/w/src/missing.ts";
  await linkifyPaths(rootOf([gone, word, fenced]), { cwd: "/w", stat });
  expect(gone.classes.size).toBe(0);
  expect(word.classes.size).toBe(0);
  expect(fenced.classes.size).toBe(0);
});

test("dedupes stats and caps the number of distinct paths", async () => {
  const els = [code("a.ts"), code("a.ts"), ...Array.from({ length: 40 }, (_, i) => code(`f${i}.ts`))];
  const asked: string[] = [];
  await linkifyPaths(rootOf(els), { stat: async (p) => (asked.push(p), true) });
  expect(asked.filter((p) => p === "a.ts").length).toBe(1);
  expect(asked.length).toBe(30);
});

test("a stale tree is not marked", async () => {
  const el = code("a.ts");
  await linkifyPaths(rootOf([el]), { stat: async () => true, live: () => false });
  expect(el.classes.size).toBe(0);
});

test("the inert variant, a streaming tail, and a chat with no opener never linkify", () => {
  expect(linkifyEnabled({ links: true, enhance: true, hasOpener: true })).toBe(true);
  expect(linkifyEnabled({ links: false, enhance: true, hasOpener: true })).toBe(false);
  expect(linkifyEnabled({ links: true, enhance: false, hasOpener: true })).toBe(false);
  expect(linkifyEnabled({ links: true, enhance: true, hasOpener: false })).toBe(false);
});
