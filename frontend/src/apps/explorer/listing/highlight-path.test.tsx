// renderHighlightPath (listing/bits.tsx) spaces and mutes "/" separators in
// a search-hit path without touching the underlying string — highlightSegments
// (platform/lib/fuzzy.ts) computes its match runs against raw character
// offsets, so the string itself must stay exactly the path it names (it still
// has to copy to the clipboard correctly). react-test-renderer: no DOM, real
// React, the same tool FilesHome.render.test.tsx uses.
import { expect, test } from "bun:test";
import { create, type ReactTestRendererJSON } from "react-test-renderer";
import { createElement } from "react";
import { renderHighlight, renderHighlightPath } from "@apps/explorer/listing/bits";

type Node = ReactTestRendererJSON | string;

function flattenText(node: Node | Node[] | null): string {
  if (node === null) return "";
  if (Array.isArray(node)) return node.map(flattenText).join("");
  if (typeof node === "string") return node;
  return flattenText(node.children ?? null);
}

function findAll(node: Node | Node[] | null, type: string): ReactTestRendererJSON[] {
  if (node === null) return [];
  if (Array.isArray(node)) return node.flatMap((n) => findAll(n, type));
  if (typeof node === "string") return [];
  const here = node.type === type ? [node] : [];
  return here.concat(findAll(node.children ?? null, type));
}

test("a match straddling a '/' stays one continuous highlight, not two", () => {
  // "ab/cd" with positions {1,2,3} matches "b", "/" and "c" — a run that
  // crosses the separator right in the middle of it.
  const tree = create(createElement("div", null, renderHighlightPath("ab/cd", [1, 2, 3])))
    .toJSON() as ReactTestRendererJSON;
  const marks = findAll(tree, "mark");
  expect(marks.length).toBe(1);
  // The separator inside that one mark is its own element, not bare text —
  // that's what lets CSS give it margin/color.
  const seps = findAll(marks[0], "span");
  expect(seps.length).toBe(1);
  expect(seps[0].props.className).toBe("path-sep");
});

test("plain, non-matching separators are still wrapped for spacing", () => {
  const tree = create(createElement("div", null, renderHighlightPath("foo/bar/baz", [])))
    .toJSON() as ReactTestRendererJSON;
  const seps = findAll(tree, "span").filter((n) => n.props.className === "path-sep");
  expect(seps.length).toBe(2);
});

test("the rendered text reconstructs to the exact original string — no character added or removed", () => {
  const original = "foo/bar/baz.txt";
  const tree = create(createElement("div", null, renderHighlightPath(original, [4, 5, 6])))
    .toJSON() as ReactTestRendererJSON;
  expect(flattenText(tree)).toBe(original);
});

test("renderHighlight (the filename-only sibling) is untouched — no separator wrapping", () => {
  const tree = create(createElement("div", null, renderHighlight("weird/name.txt", [])))
    .toJSON() as ReactTestRendererJSON;
  const seps = findAll(tree, "span").filter((n) => n.props.className === "path-sep");
  expect(seps.length).toBe(0);
  expect(flattenText(tree)).toBe("weird/name.txt");
});
