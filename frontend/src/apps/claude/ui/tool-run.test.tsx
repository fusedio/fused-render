// A folded run of tool calls (design.md §A) — the header it wears and the keys
// its nested chips keep.
//
// `react-test-renderer`, the `chip-copy.test.tsx` pattern: no DOM, so a chip's
// open state is read off the collapse policy rather than off a rendered panel.
import { afterEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestRendererJSON } from "react-test-renderer";

import { CardPolicyProvider, createCardPolicy } from "./cardPolicy";
import { SegmentView } from "./SegmentView";
import { ToolRunChip } from "./ToolRunChip";
import type { Segment, ToolSegment } from "../protocol/types";

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

function byClass(node: Json | Json[] | null, cls: string): Json[] {
  const out: Json[] = [];
  const walk = (n: Json | string | null) => {
    if (!n || typeof n === "string") return;
    const c = (n.props as { className?: string } | undefined)?.className;
    if (typeof c === "string" && c.split(" ").includes(cls)) out.push(n);
    for (const k of n.children ?? []) walk(k as Json | string);
  };
  for (const n of Array.isArray(node) ? node : [node]) walk(n);
  return out;
}

/** Every string in a subtree, joined — the row's words as a reader sees them. */
function words(node: Json | null): string {
  const out: string[] = [];
  const walk = (n: Json | string | null) => {
    if (!n) return;
    if (typeof n === "string") {
      out.push(n);
      return;
    }
    for (const k of n.children ?? []) walk(k as Json | string);
  };
  walk(node);
  return out.join("");
}

const tool = (id: string, name: string, status: ToolSegment["status"] = "ok"): ToolSegment => ({
  kind: "tool",
  id,
  name,
  input: name === "Bash" ? { command: "ls -la" } : { file_path: "/tmp/a.ts" },
  status,
  output: "",
  images: [],
});

describe("ToolRunChip", () => {
  test("collapsed header: just the count", () => {
    const segs = [tool("a", "Read"), tool("b", "Read"), tool("c", "Bash")];
    const r = mount(
      <CardPolicyProvider value={createCardPolicy()}>
        <ToolRunChip segs={segs} start={2} seq={9} />
      </CardPolicyProvider>,
    );
    const json = r.toJSON() as Json;
    expect(words(byClass(json, "chip-name")[0]!)).toBe("3 tool calls");
    // Collapsed, so no nested chip is mounted yet.
    expect(byClass(json, "toolchip").filter((n) => !(n.props as { className: string }).className.includes("toolrun"))).
      toHaveLength(0);
  });

  test("an error anywhere is the run's status; otherwise it is done", () => {
    const bad = mount(
      <ToolRunChip segs={[tool("a", "Read"), tool("b", "Bash", "error")]} start={0} seq={1} />,
    ).toJSON() as Json;
    expect((byClass(bad, "chip-status")[0]!.props as { className: string }).className).toContain("is-error");
    const ok = mount(
      <ToolRunChip segs={[tool("a", "Read"), tool("b", "Bash")]} start={0} seq={2} />,
    ).toJSON() as Json;
    expect((byClass(ok, "chip-status")[0]!.props as { className: string }).className).toContain("is-ok");
  });

  test("an OPEN run mounts the N chips under their ORIGINAL card keys", () => {
    const policy = createCardPolicy();
    // The reader's click, as `chip-copy.test.tsx` records it: the override map
    // IS the open state, and there is no DOM here to dispatch a real event on.
    // `run:` over the first chip's own key — never the chip's key itself, or
    // opening the run would open its first chip too.
    policy.overrides.set("run:tool:a", true);
    const segs = [tool("a", "Read"), tool("b", "Bash"), tool("c", "Grep")];
    const r = mount(
      <CardPolicyProvider value={policy}>
        <ToolRunChip segs={segs} start={4} seq={9} />
      </CardPolicyProvider>,
    );
    const open = r.toJSON() as Json;
    // The header stays put as the collapse bar, and the three chips are under it.
    expect(words(byClass(open, "chip-name")[0]!)).toBe("3 tool calls");
    expect((byClass(open, "toolrun")[0]!.props as { className: string }).className).toContain("is-open");
    const inner = byClass(open, "toolchip").filter(
      (n) => !(n.props as { className: string }).className.includes("toolrun"),
    );
    expect(inner).toHaveLength(3);
    // ...each still folded, under the key it had before the run existed.
    expect(byClass(open, "chip-body")).toHaveLength(1);
    policy.overrides.set("tool:c", true);
    act(() => {
      r.update(
        <CardPolicyProvider value={policy}>
          <ToolRunChip segs={segs} start={4} seq={9} key="again" />
        </CardPolicyProvider>,
      );
    });
    expect(byClass(r.toJSON() as Json, "chip-body")).toHaveLength(2);
  });
});

describe("SegmentView folds runs", () => {
  const text = (t: string): Segment => ({ kind: "text", text: t });

  test("a settled stretch folds; a running one stays individual", () => {
    const settled: Segment[] = [text("hi"), tool("a", "Read"), tool("b", "Bash")];
    const folded = mount(
      <CardPolicyProvider value={createCardPolicy()}>
        <SegmentView segments={settled} />
      </CardPolicyProvider>,
    ).toJSON() as Json | Json[];
    expect(byClass(folded, "toolrun")).toHaveLength(1);

    const live: Segment[] = [text("hi"), tool("a", "Read"), tool("b", "Bash", "running")];
    const loose = mount(
      <CardPolicyProvider value={createCardPolicy()}>
        <SegmentView segments={live} />
      </CardPolicyProvider>,
    ).toJSON() as Json | Json[];
    expect(byClass(loose, "toolrun")).toHaveLength(0);
    expect(byClass(loose, "toolchip")).toHaveLength(2);
  });

  test("a live turn's trailing stretch stays individual until the turn ends", () => {
    // `Turn` hands `live={!!turn.streaming}` down. While it is on, the stretch
    // at the end of the list can still grow, so it is not folded — otherwise
    // the lid goes on and comes off once per tool for the length of the run.
    const segs: Segment[] = [text("hi"), tool("a", "Read"), tool("b", "Bash")];
    const open = mount(
      <CardPolicyProvider value={createCardPolicy()}>
        <SegmentView segments={segs} live />
      </CardPolicyProvider>,
    ).toJSON() as Json | Json[];
    expect(byClass(open, "toolrun")).toHaveLength(0);
    expect(byClass(open, "toolchip")).toHaveLength(2);
  });

  test("a filed card still sits under its own chip, and the run ends before it", () => {
    const segs: Segment[] = [
      tool("a", "Read"),
      tool("b", "Read"),
      tool("c", "Bash"),
      tool("d", "Grep"),
      tool("e", "Glob"),
    ];
    const cardsAfter = new Map<number, React.ReactNode>([[2, <div key="card" className="filed-card" />]]);
    const json = mount(
      <CardPolicyProvider value={createCardPolicy()}>
        <SegmentView segments={segs} cardsAfter={cardsAfter} />
      </CardPolicyProvider>,
    ).toJSON() as Json | Json[];
    expect(byClass(json, "toolrun")).toHaveLength(2);
    expect(byClass(json, "filed-card")).toHaveLength(1);
  });
});
