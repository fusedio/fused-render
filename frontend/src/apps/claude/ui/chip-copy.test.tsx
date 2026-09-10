// EVERY `pre` IN A TOOL CHIP CARRIES T'S COPY BUTTON — visual pass 3, FIX-23.
//
// T runs `attachCodeCopy` over the whole rendered body (T:15122
// `attachCodeCopy(bodyEl.parentElement)`), chips included: 2 buttons per chip,
// 7 in the transcript the pass measured. This port only ever called
// `enhanceCodeBlocks` from `MarkdownView`, and a chip body is not markdown, so
// the command a tool ran and the output it got back could not be copied at all.
//
// Measured live before the fix: `.toolchip .chip-body .copybtn` = 0 with six
// chips open and ten `pre` on screen.
//
// react-test-renderer, the cards.test.tsx pattern: no DOM, so the button's
// `onClick` is driven as a prop. That is deliberate — the FIRST attempt at this
// fix walked the mounted panel from a `useEffect` and could not be tested here
// at all, and it did not work either: `CollapsibleContent` is a plain function
// wrapper over Base UI's forward-ref Panel, and React 18 drops a `ref` passed
// to a plain function component. Silently. The count stayed 0 while a source-
// reading test happily confirmed the wiring was "there".
import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRendererJSON } from "react-test-renderer";

import { createCardPolicy, CardPolicyProvider } from "./cardPolicy";
import { ToolChip } from "./ToolChip";
import type { ToolSegment } from "../protocol/types";

const mounted: Array<ReturnType<typeof create>> = [];
function mount(el: React.ReactElement): ReturnType<typeof create> {
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(el);
  });
  mounted.push(r);
  return r;
}
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
});

type Json = ReactTestRendererJSON;

/** Every node in the tree whose props carry `className === cls`. */
function byClass(node: Json | Json[] | null, cls: string): Json[] {
  const out: Json[] = [];
  const walk = (n: Json | string | null) => {
    if (!n || typeof n === "string") return;
    const c = (n.props as { className?: string } | undefined)?.className;
    if (c === cls) out.push(n);
    for (const k of n.children ?? []) walk(k as Json | string);
  };
  for (const n of Array.isArray(node) ? node : [node]) walk(n);
  return out;
}

function byType(node: Json | Json[] | null, type: string): Json[] {
  const out: Json[] = [];
  const walk = (n: Json | string | null) => {
    if (!n || typeof n === "string") return;
    if (n.type === type) out.push(n);
    for (const k of n.children ?? []) walk(k as Json | string);
  };
  for (const n of Array.isArray(node) ? node : [node]) walk(n);
  return out;
}

/** A chip mounted ALREADY OPEN. Every collapsible in the transcript ships
 *  collapsed and its content is unmounted while closed (A GAP-D10), so the
 *  policy is pre-seeded rather than the trigger driven: the reader's click is
 *  `cardPolicy`'s business and is tested there. Its own policy per mount, so
 *  one test's override cannot decide the next one's. */
function open(seg: Partial<ToolSegment>): ReturnType<typeof create> {
  const key = "k" + Math.random();
  const policy = createCardPolicy();
  policy.overrides.set(key, true);
  return mount(
    <CardPolicyProvider value={policy}>
      <ToolChip
        seg={
          {
            kind: "tool",
            id: key,
            name: "Bash",
            status: "done",
            input: {},
            output: null,
            images: [],
            ...seg,
          } as ToolSegment
        }
        cardKey={key}
      />
    </CardPolicyProvider>,
  );
}

const chip = (seg: Partial<ToolSegment>): Json | Json[] | null => open(seg).toJSON();

test("a Bash chip's command pre carries span.copywrap > button.copybtn, FIRST", () => {
  const json = chip({ name: "Bash", input: { command: "npm run build" } });
  const pres = byType(json, "pre");
  expect(pres.length).toBeGreaterThan(0);
  const first = pres[0]!;
  const wrap = first.children?.[0] as Json;
  // FIRST child, not last: `.copywrap` is a zero-height anchor and the button
  // inside it is `position: absolute` against the `pre`.
  expect(wrap.type).toBe("span");
  expect((wrap.props as { className?: string }).className).toBe("copywrap");
  const btn = wrap.children?.[0] as Json;
  expect(btn.type).toBe("button");
  expect((btn.props as { className?: string }).className).toBe("copybtn");
  // `type="button"`, or a chip inside a form-bearing card would submit it.
  expect((btn.props as { type?: string }).type).toBe("button");
  expect(btn.children?.[0]).toBe("copy");
});

test("the label goes to `copied` on the press and the string is the pre's own", () => {
  let wrote = "";
  type Nav = { clipboard?: { writeText(t: string): Promise<void> } };
  const nav = globalThis.navigator as Nav | undefined;
  if (!nav) {
    Object.defineProperty(globalThis, "navigator", { value: {}, configurable: true, writable: true });
  }
  const prev = (globalThis.navigator as Nav).clipboard;
  (globalThis.navigator as Nav).clipboard = {
    writeText: (t: string) => {
      wrote = t;
      return Promise.resolve();
    },
  };
  try {
    const r = open({ input: { command: "ls -la" } });
    const btn = byClass(r.toJSON(), "copybtn")[0]!;
    act(() => {
      (btn.props as { onClick: () => void }).onClick();
    });
    expect(wrote).toBe("ls -la");
    expect(byClass(r.toJSON(), "copybtn")[0]!.children?.[0]).toBe("copied");
  } finally {
    (globalThis.navigator as Nav).clipboard = prev;
  }
});

test("an Edit chip's diff copies the LINES, joined — not the DOM's runs", () => {
  // T:15368-15393 renders one span per line with NO "\n" text nodes between
  // them (the spans are `display: block`, which is what makes the +/- band
  // full-width), so reading the node back would join a whole diff into one
  // line. T's own `copyText` joins `:scope > span`; here the caller already
  // has the strings.
  const json = chip({
    name: "Edit",
    input: { file_path: "/tmp/a.txt", old_string: "one", new_string: "two" },
  });
  const btn = byClass(json, "copybtn")[0];
  expect(btn).toBeDefined();
  const diff = byType(json, "pre").find((p) =>
    ((p.props as { className?: string }).className ?? "").startsWith("diff"),
  );
  expect(diff).toBeDefined();
  // The spans survive the wrap: the button is an extra FIRST child and nothing
  // else about the diff changed.
  const spans = byType(diff!, "span").filter(
    (sp) => (sp.props as { className?: string }).className !== "copywrap",
  );
  expect(spans.length).toBeGreaterThan(1);
});

test("a chip with an output block gets a button on THAT pre too — T's 2 per chip", () => {
  const json = chip({
    name: "Bash",
    input: { command: "echo hi" },
    output: "hi\n",
  });
  const pres = byType(json, "pre");
  expect(pres.length).toBe(2);
  // Every `pre` in the body, not just the first: legacy counted 2 per chip and
  // this port counted 0.
  for (const p of pres) {
    const wrap = p.children?.[0] as Json;
    expect((wrap.props as { className?: string }).className).toBe("copywrap");
  }
  expect(byClass(json, "copybtn")).toHaveLength(2);
});
