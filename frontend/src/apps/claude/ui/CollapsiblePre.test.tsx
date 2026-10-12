// Card payloads clip-and-expand instead of scrolling (2026-10-12): a nested
// scroller inside the pinned card trapped trackpad swipes and hid Allow/Deny.
// These pin the honest half of that trade — a clipped payload always says so,
// and the whole text is one real button away.
import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRendererJSON } from "react-test-renderer";

import { CollapsiblePre, hiddenLineCount, toggleLabel } from "./CollapsiblePre";
import { PermCard } from "./PermCard";
import { PlanCard } from "./PlanCard";
import { QuestionCard } from "./QuestionCard";
import type { PermissionRow } from "../protocol/types";

type Json = ReactTestRendererJSON;
// eslint-disable-next-line @typescript-eslint/no-explicit-any -- host props are untyped
type Props = Record<string, any>;
type Renderer = ReturnType<typeof create>;

const mounted: Renderer[] = [];
function mount(el: React.ReactElement, nodeMock?: Props): Renderer {
  let r!: Renderer;
  act(() => {
    r = create(el, nodeMock ? { createNodeMock: () => nodeMock } : undefined);
  });
  mounted.push(r);
  return r;
}
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
});

function walk(node: Json | Json[] | string | null, hit: (n: Json) => void): void {
  if (!node || typeof node === "string") return;
  if (Array.isArray(node)) return node.forEach((n) => walk(n, hit));
  hit(node);
  for (const k of node.children ?? []) walk(k as Json, hit);
}
function textOf(node: Json | string | null | undefined): string {
  if (!node) return "";
  if (typeof node === "string") return node;
  return (node.children ?? []).map((k) => textOf(k as Json)).join("");
}
function all(r: Renderer, type: string): Json[] {
  const out: Json[] = [];
  walk(r.toJSON() as Json | Json[], (n) => {
    if (n.type === type) out.push(n);
  });
  return out;
}
function toggles(r: Renderer): Json[] {
  return all(r, "button").filter((b) => String((b.props as Props).className).includes("perm-pre-toggle"));
}
function classes(n: Json): string[] {
  return String((n.props as Props).className ?? "").split(" ");
}

const lines = (n: number) => Array.from({ length: n }, (_, i) => `echo line ${i + 1}`).join("\n");

function row(over: Partial<PermissionRow> = {}): PermissionRow {
  return {
    id: "req-1",
    tool: "Bash",
    input: { command: "ls" },
    created_at: 0,
    decision: "",
    scope: "",
    mode: "",
    answers: {},
    ...over,
  };
}
const noop = async () => {};

test("line counting: six lines fit, a trailing newline is not a line", () => {
  expect(hiddenLineCount("a")).toBe(0);
  expect(hiddenLineCount(lines(6))).toBe(0);
  expect(hiddenLineCount(lines(6) + "\n")).toBe(0);
  expect(hiddenLineCount(lines(7))).toBe(1);
  expect(hiddenLineCount(lines(20))).toBe(14);
  expect(toggleLabel("command", 1, false)).toBe("Show full command (1 more line)");
  expect(toggleLabel("command", 0, false)).toBe("Show full command");
  expect(toggleLabel("command", 14, true)).toBe("Show less");
});

test("a short Bash command shows whole, with no disclosure button", () => {
  const r = mount(<PermCard row={row({ input: { command: "ls -la" } })} onDecide={noop} />);
  expect(toggles(r)).toHaveLength(0);
  const pre = all(r, "pre")[0]!;
  expect(textOf(pre)).toBe("ls -la");
  expect(classes(pre)).not.toContain("is-clipped");
  // Allow/Deny are there.
  expect(all(r, "button").map((b) => textOf(b))).toContain("Deny");
});

test("a 20-line command collapses with a count, expands in place, and collapses again", () => {
  const cmd = lines(20);
  const r = mount(<PermCard row={row({ input: { command: cmd } })} onDecide={noop} />);
  const [btn] = toggles(r);
  expect(textOf(btn)).toBe("Show full command (14 more lines)");
  expect((btn!.props as Props).type).toBe("button");
  expect((btn!.props as Props)["aria-expanded"]).toBe(false);
  let pre = all(r, "pre")[0]!;
  // The whole text is in the DOM even while clipped — the clip is visual.
  expect(textOf(pre)).toBe(cmd);
  expect(classes(pre)).toEqual(expect.arrayContaining(["is-collapsed", "is-clipped"]));
  expect((btn!.props as Props)["aria-controls"]).toBe((pre.props as Props).id);

  act(() => (toggles(r)[0]!.props as Props).onClick());
  pre = all(r, "pre")[0]!;
  expect(textOf(pre)).toBe(cmd);
  expect(classes(pre)).not.toContain("is-collapsed");
  expect(classes(pre)).not.toContain("is-clipped");
  expect(textOf(toggles(r)[0])).toBe("Show less");
  expect((toggles(r)[0]!.props as Props)["aria-expanded"]).toBe(true);

  act(() => (toggles(r)[0]!.props as Props).onClick());
  expect(classes(all(r, "pre")[0]!)).toContain("is-clipped");
});

test("one long line that WRAPS past the cap is measured and gets an uncounted button", () => {
  const long = "x".repeat(2000);
  // The pre reports more content than its clipped box.
  const r = mount(<CollapsiblePre text={long} />, { scrollHeight: 900, clientHeight: 110 });
  expect(textOf(toggles(r)[0])).toBe("Show full command");
  expect(classes(all(r, "pre")[0]!)).toContain("is-clipped");

  const fits = mount(<CollapsiblePre text="short" />, { scrollHeight: 30, clientHeight: 30 });
  expect(toggles(fits)).toHaveLength(0);
});

test("a non-Bash payload says 'input', and the leftover JSON dump collapses too", () => {
  const big = Object.fromEntries(Array.from({ length: 12 }, (_, i) => [`k${i}`, i]));
  const r = mount(
    <PermCard row={row({ tool: "mcp__x__y", input: big })} onDecide={noop} />,
  );
  const labels = toggles(r).map((b) => textOf(b));
  expect(labels.length).toBeGreaterThan(0);
  expect(labels.every((l) => l.startsWith("Show full input ("))).toBe(true);
});

test("PlanCard and QuestionCard extra dumps collapse; the cards still render", () => {
  const extra = Object.fromEntries(Array.from({ length: 12 }, (_, i) => [`k${i}`, i]));
  const p = mount(
    <PlanCard row={row({ tool: "ExitPlanMode", input: { plan: "do it", ...extra } })} onDecide={noop} />,
  );
  expect(textOf(toggles(p)[0])).toMatch(/^Show full input \(\d+ more lines\)$/);
  expect(all(p, "button").length).toBeGreaterThan(1);

  const q = mount(
    <QuestionCard
      row={row({
        tool: "AskUserQuestion",
        input: { questions: [{ question: "Which one?", options: [{ label: "A" }, { label: "B" }] }], ...extra },
      })}
      onAnswer={noop}
      onDismiss={() => {}}
    />,
  );
  expect(textOf(toggles(q)[0])).toMatch(/^Show full input \(\d+ more lines\)$/);
  expect(textOf(q.toJSON() as Json)).toContain("Which one?");

  // The unanswerable fallback dumps the whole input through the same component.
  const bad = mount(
    <QuestionCard row={row({ tool: "AskUserQuestion", input: extra })} onAnswer={noop} onDismiss={() => {}} />,
  );
  expect(textOf(toggles(bad)[0])).toMatch(/^Show full input/);
});
