// PR #1336 review (Bugbot, "Portal leaves styles in the iframe"): a dialog
// portaled into the top document must get the iframe's stylesheets there.
// Fake documents, since the bun DOM shim has no real tree.
import { expect, test } from "bun:test";

import { mirrorStylesheets } from "./topContainer";

interface FakeNode {
  tagName: string;
  href?: string;
  rel?: string;
  textContent?: string;
  attrs: Record<string, string>;
  setAttribute(k: string, v: string): void;
  hasAttribute(k: string): boolean;
}

function node(tagName: string, init: Partial<FakeNode> = {}): FakeNode {
  const attrs: Record<string, string> = {};
  return {
    tagName,
    attrs,
    setAttribute: (k, v) => {
      attrs[k] = v;
    },
    hasAttribute: (k) => k in attrs,
    ...init,
  };
}

function fakeDoc(initial: FakeNode[]) {
  const nodes = [...initial];
  const head = { appendChild: (n: FakeNode) => (nodes.push(n), n) };
  return {
    nodes,
    head,
    documentElement: head,
    querySelectorAll: () => [...nodes],
    createElement: (tag: string) => node(tag.toUpperCase()),
  } as unknown as Document & { nodes: FakeNode[] };
}

test("copies the iframe's links and inline styles into the target document", () => {
  const from = fakeDoc([
    node("LINK", { href: "http://x/chat.css", rel: "stylesheet" }),
    node("STYLE", { textContent: ".a{color:red}" }),
  ]);
  const to = fakeDoc([]);
  mirrorStylesheets(from, to);
  expect(to.nodes.map((n) => [n.tagName, n.href ?? n.textContent])).toEqual([
    ["LINK", "http://x/chat.css"],
    ["STYLE", ".a{color:red}"],
  ]);
});

test("is idempotent and skips sheets the target already has", () => {
  const from = fakeDoc([
    node("LINK", { href: "http://x/chat.css", rel: "stylesheet" }),
    node("LINK", { href: "http://x/other.css", rel: "stylesheet" }),
    node("STYLE", { textContent: ".a{color:red}" }),
  ]);
  const to = fakeDoc([node("LINK", { href: "http://x/chat.css", rel: "stylesheet" })]);
  mirrorStylesheets(from, to);
  const afterFirst = to.nodes.length;
  expect(afterFirst).toBe(3); // chat.css was already there
  mirrorStylesheets(from, to);
  expect(to.nodes.length).toBe(afterFirst);
});
