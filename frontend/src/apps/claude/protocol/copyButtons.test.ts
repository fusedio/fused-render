// T:14998 `attachCodeCopy`, the `pre`-walking half — visual pass 3, FIX-23.
//
// Legacy runs it over the whole rendered body (T:15122
// `attachCodeCopy(bodyEl.parentElement)`), TOOL CHIPS INCLUDED: 2 buttons per
// chip, 7 in the transcript the pass measured. This port only ever called it
// from `MarkdownView`, and a chip body is not markdown — `ui/ToolChip.tsx`
// builds its `pre` from JSX — so every command echo and every output block in
// every chip had exactly 0, with no way out but a manual selection inside a
// 267px box. The CSS had been ported the whole time; only the element was
// missing.
//
// NO REAL DOM HERE (the suite runs under bun with `testDomShim`'s stub), so the
// nodes below are the smallest fake that answers the four calls the walker
// makes. That is enough for what regressed: which element goes in, where, and
// that a second pass is a no-op.
import { beforeEach, expect, test } from "bun:test";
import { readFileSync } from "node:fs";

interface FakeNode {
  className: string;
  tagName: string;
  textContent: string;
  type?: string;
  onclick?: () => void;
  children: FakeNode[];
  isConnected: boolean;
  firstChild: FakeNode | null;
  querySelector(sel: string): FakeNode | null;
  querySelectorAll(sel: string): FakeNode[];
  appendChild(n: FakeNode): void;
  insertBefore(n: FakeNode, before: FakeNode | null): void;
}

function node(tagName: string, textContent = ""): FakeNode {
  const self: FakeNode = {
    className: "",
    tagName,
    textContent,
    children: [],
    isConnected: true,
    get firstChild() {
      return self.children[0] ?? null;
    },
    querySelector(sel) {
      return self.querySelectorAll(sel)[0] ?? null;
    },
    querySelectorAll(sel) {
      const want = sel.replace(/^:scope > /, "").replace(/^\./, "");
      const byClass = sel.startsWith(".");
      const out: FakeNode[] = [];
      const walk = (n: FakeNode) => {
        for (const c of n.children) {
          if (byClass ? c.className.split(" ").includes(want) : c.tagName === want) out.push(c);
          walk(c);
        }
      };
      walk(self);
      return out;
    },
    appendChild(n) {
      self.children.push(n);
    },
    insertBefore(n, before) {
      const at = before ? self.children.indexOf(before) : -1;
      if (at < 0) self.children.push(n);
      else self.children.splice(at, 0, n);
    },
  };
  return self;
}

// `attachCopyButtons` reaches for `document.createElement`, and nothing else.
(globalThis as { document?: unknown }).document = {
  createElement: (tag: string) => node(tag),
};

const { attachCopyButtons } = await import("./markdown");

let root: FakeNode;
let pre: FakeNode;
beforeEach(() => {
  root = node("div");
  root.className = "chip-body";
  pre = node("pre");
  pre.appendChild(node("code", "npm run build"));
  root.appendChild(pre);
});

test("every pre in a chip body gets T's span.copywrap > button.copybtn, FIRST", () => {
  attachCopyButtons(root as unknown as ParentNode);
  const wrap = pre.children[0]!;
  // FIRST child, not last: `.copywrap` is a zero-height anchor and the button
  // inside it is `position: absolute` against the `pre` — appended at the end
  // it would sit under the code it is meant to sit over.
  expect(wrap.tagName).toBe("span");
  expect(wrap.className).toBe("copywrap");
  const btn = wrap.children[0]!;
  expect(btn.tagName).toBe("button");
  expect(btn.className).toBe("copybtn");
  // `type="button"`, or a chip inside a form-bearing card would submit it.
  expect(btn.type).toBe("button");
  expect(btn.textContent).toBe("copy");
});

test("it is idempotent, so an open chip may run it on every paint", () => {
  // The chip calls it from an effect keyed on the open state AND the segment: a
  // running tool's output grows under a body that is already open, and a
  // re-render mid-copy must not reset a "copied" label by re-inserting.
  attachCopyButtons(root as unknown as ParentNode);
  attachCopyButtons(root as unknown as ParentNode);
  attachCopyButtons(root as unknown as ParentNode);
  expect(root.querySelectorAll(".copybtn")).toHaveLength(1);
  expect(root.querySelectorAll(".copywrap")).toHaveLength(1);
});

test("a body with several pre gets one button each — legacy's 2 per chip", () => {
  const second = node("pre");
  second.appendChild(node("code", "ok\n"));
  root.appendChild(second);
  attachCopyButtons(root as unknown as ParentNode);
  expect(root.querySelectorAll(".copybtn")).toHaveLength(2);
});

test("ToolChip still wires the walker over its own body", () => {
  // The whole defect was a missing CALL, so the call is what this pins: a
  // stylesheet test cannot see it and the render tests run without a DOM.
  const src = readFileSync(new URL("../ui/ToolChip.tsx", import.meta.url), "utf8");
  expect(src).toContain("attachCopyButtons");
  expect(src).toMatch(/ref=\{bodyRef\}/);
  // Keyed on BOTH, for the two ways a chip body's `pre` set changes.
  expect(src).toMatch(/\[open, seg\]/);
});
