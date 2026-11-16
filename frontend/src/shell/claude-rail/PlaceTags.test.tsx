// THE PLACE TAGS, APART (PlaceTags `part`; Akshil, 2026-11-16): the unified
// head seats the place's name at the left (the favourite's window alone) and
// where in it at the right, with its icon and no chevron. The two-row head
// keeps both side by side.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { expect, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestRenderer, type ReactTestRendererJSON } from "react-test-renderer";
import type { RailPlace, RailScope } from "./claude-rail-lib";

const { PlaceTags } = await import("./PlaceTags");
type Props = Parameters<typeof PlaceTags>[0];

const canvas: RailPlace = { kind: "canvas", env: "prod", name: "HLS_Data", dir: "/c/HLS_Data" };
const folder: RailPlace = { kind: "folder", dir: "/a/app/" };
const node: RailScope = { kind: "node", name: "hex", file: "hex.py" };

function flat(el: Props): ReactTestRendererJSON[] {
  let r!: ReactTestRenderer;
  act(() => {
    r = create(createElement(PlaceTags, el));
  });
  const out: ReactTestRendererJSON[] = [];
  const walk = (n: ReactTestRendererJSON | ReactTestRendererJSON[] | string | null) => {
    if (!n || typeof n === "string") return;
    if (Array.isArray(n)) return n.forEach(walk);
    out.push(n);
    (n.children ?? []).forEach((c) => walk(c as ReactTestRendererJSON | string));
  };
  walk(r.toJSON());
  act(() => r.unmount());
  return out;
}
const cls = (n: ReactTestRendererJSON) => String(n.props.className ?? "").split(" ");
const tags = (nodes: ReactTestRendererJSON[]) => nodes.filter((n) => cls(n).includes("place-tag"));
const text = (n: ReactTestRendererJSON): string =>
  (n.children ?? []).map((c) => (typeof c === "string" ? c : text(c))).join("");

test("no part: both tags, a chevron between, no icon (the two-row head)", () => {
  const nodes = flat({ scope: node, at: canvas, pageRoot: null });
  expect(tags(nodes).map(text)).toEqual(["HLS_Data", "hex"]);
  expect(nodes.some((n) => cls(n).includes("place-tags-sep"))).toBe(true);
  expect(nodes.some((n) => cls(n).includes("place-tag-word"))).toBe(false);
});

test('part="place": the place\'s name alone, no chevron, no icon', () => {
  const nodes = flat({ scope: node, at: canvas, pageRoot: null, part: "place" });
  expect(tags(nodes).map(text)).toEqual(["HLS_Data"]);
  expect(nodes.some((n) => cls(n).includes("place-tags-sep"))).toBe(false);
  expect(nodes.some((n) => n.type === "svg")).toBe(false);
});

test('part="where": the node tag alone — its icon, then its word; the door as it was', () => {
  const nodes = flat({ scope: node, at: canvas, pageRoot: null, sessionId: "s1", part: "where" });
  const [tag, ...rest] = tags(nodes);
  expect(rest).toHaveLength(0);
  expect(cls(tag!)).toContain("has-icon");
  expect(tag!.props.href).toBe("/canvases/prod/HLS_Data?node=hex&node_file=hex.py&session_id=s1&focus=node");
  const kids = (tag!.children ?? []) as ReactTestRendererJSON[];
  expect(kids[0]!.type).toBe("svg");
  expect(cls(kids[1]!)).toContain("place-tag-word");
  expect(text(kids[1]!)).toBe("hex");
  expect(nodes.some((n) => cls(n).includes("place-tags-sep"))).toBe(false);
});

test('part="where" on a whole canvas: the Canvas tag, with the canvas mark', () => {
  const nodes = flat({ scope: { kind: "whole", word: "Canvas" }, at: canvas, pageRoot: null, part: "where" });
  const [tag] = tags(nodes);
  expect(text(tag!)).toBe("Canvas");
  expect(cls(tag!)).toContain("is-canvas");
  expect(((tag!.children ?? []) as ReactTestRendererJSON[])[0]!.type).toBe("svg");
});

test('part="where": a file under the folder is its name alone — icons are the canvas\'s only; a whole folder draws nothing', () => {
  const file = flat({
    scope: { kind: "file", name: "index.html", file: "/a/app/index.html" },
    at: folder,
    pageRoot: "folder:/a/app",
    part: "where",
  });
  expect(tags(file).map(text)).toEqual(["index.html"]);
  expect(file.some((n) => n.type === "svg")).toBe(false);
  expect(cls(tags(file)[0]!)).not.toContain("has-icon");
  // No word seat, so the ladder has nothing to fold: the name always stands.
  expect(file.some((n) => cls(n).includes("place-tag-word"))).toBe(false);
  expect(flat({ scope: { kind: "whole", word: "Folder" }, at: folder, pageRoot: null, part: "where" })).toHaveLength(0);
});
