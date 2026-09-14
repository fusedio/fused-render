// The `more ▸` trigger and the run it opens (design.md §A) — where the word
// sits, which stretches get one, and what comes out when it is clicked.
//
// `react-test-renderer`, the `chip-copy.test.tsx` pattern: no real DOM, so a
// run's open state is read off the collapse policy rather than off a rendered
// panel.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestRendererJSON } from "react-test-renderer";

import { CardPolicyProvider, createCardPolicy } from "./cardPolicy";
import { SegmentView } from "./SegmentView";
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
    if (typeof c === "string" && c.split(/\s+/).includes(cls)) out.push(n);
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

/** The markdown each `MarkdownView` rendered, tags stripped. */
function proses(node: Json | Json[] | null): string[] {
  const out: string[] = [];
  const walk = (n: Json | string | null) => {
    if (!n || typeof n === "string") return;
    const h = (n.props as { dangerouslySetInnerHTML?: { __html: string } } | undefined)
      ?.dangerouslySetInnerHTML;
    if (h) out.push(h.__html.replace(/<[^>]*>/g, "").trim());
    for (const k of n.children ?? []) walk(k as Json | string);
  };
  for (const n of Array.isArray(node) ? node : [node]) walk(n);
  return out;
}

const text = (t: string): Segment => ({ kind: "text", text: t });
const think = (t: string): Segment => ({ kind: "thinking", text: t });
const notice = (t: string): Segment => ({ kind: "notice", text: t }) as Segment;
const tool = (id: string, name: string, status: ToolSegment["status"] = "ok"): ToolSegment => ({
  kind: "tool",
  id,
  name,
  input: name === "Bash" ? { command: "ls -la" } : { file_path: "/tmp/a.ts" },
  status,
  output: "",
  images: [],
});

const view = (segs: Segment[], props: Record<string, unknown> = {}, policy = createCardPolicy()) =>
  mount(
    <CardPolicyProvider value={policy}>
      <SegmentView segments={segs} {...props} />
    </CardPolicyProvider>,
  );

/** Chips that are NOT nested inside anything else — the members on screen. */
const chips = (json: Json | Json[] | null) => byClass(json, "toolchip");

describe("the trigger's seat", () => {
  test("it sits in the corner of the prose block the run follows", () => {
    const json = view([text("Here goes."), tool("a", "Read"), tool("b", "Bash")]).toJSON() as Json | Json[];
    const blocks = byClass(json, "seg-block");
    expect(blocks).toHaveLength(1);
    // The prose and the trigger are ONE element: the word is drawn in the last
    // line's own box, not on a row of its own under it.
    expect(proses(blocks[0]!)).toEqual(["Here goes."]);
    expect(words(byClass(blocks[0]!, "run-trigger")[0]!)).toBe("more▸");
    expect((blocks[0]!.props as { className: string }).className).not.toContain("is-bare");
    // Collapsed: not one chip is mounted.
    expect(chips(json)).toHaveLength(0);
  });

  test("no prose before the run → the trigger takes its own right-aligned line", () => {
    // A turn that opens on a tool call (design.md §A, Q1).
    const json = view([tool("a", "Read"), text("Done.")]).toJSON() as Json | Json[];
    const block = byClass(json, "seg-block")[0]!;
    expect((block.props as { className: string }).className).toContain("is-bare");
    expect(proses(block)).toEqual([]);
    // The prose AFTER the run is untouched — it is not the trigger's seat.
    expect(proses(json)).toEqual(["Done."]);
  });

  test("the growing tail never carries a trigger", () => {
    // The typer rewrites that element per frame; a control inside it would be
    // inside what is being rewritten. (Reachable only for a turn whose tail is
    // followed by a closed stretch — the tail is prose, so it is never a run.)
    const segs = [text("streaming…"), tool("a", "Read")];
    const json = view(segs, { tail: { index: 0, text: "stre", cursor: true } }).toJSON() as
      | Json
      | Json[];
    const blocks = byClass(json, "seg-block");
    // TWO blocks: the tail has one of its own (every prose segment does, review
    // #4) and it holds the caret, not a trigger; the run takes a bare one.
    expect(blocks).toHaveLength(2);
    expect((blocks[0]!.props as { className: string }).className).not.toContain("has-trigger");
    expect(byClass(blocks[0]!, "run-trigger")).toHaveLength(0);
    expect((blocks[1]!.props as { className: string }).className).toContain("is-bare");
    expect(byClass(blocks[1]!, "run-trigger")).toHaveLength(1);
  });

  test("THE PROSE ELEMENT SURVIVES THE RUN UN-SUPPRESSING (review #4)", () => {
    // A live turn holds its trailing stretch open (no trigger); the frame it
    // settles in, the stretch folds and the trigger appears. The trigger used
    // to be drawn by a component that took the prose node back off the list and
    // re-parented it — so at that exact frame every paragraph before a run
    // remounted: markdown re-parsed, hljs re-run, the reader's selection and the
    // copy button's state gone.
    //
    // MOUNTS ARE COUNTED, not inspected: `createNodeMock` is called once per
    // host mount, so the prose div showing up twice IS the remount.
    const seen: string[] = [];
    const nodeMock = (el: { props: { className?: string } }) => {
      if (el.props.className === "seg-text") seen.push("mount");
      return null;
    };
    const segs = [text("Here goes."), tool("a", "Read"), tool("b", "Bash")];
    const policy = createCardPolicy();
    const live = (
      <CardPolicyProvider value={policy}>
        <SegmentView segments={segs} live tail={{ index: 0, text: "Here goes.", cursor: true }} />
      </CardPolicyProvider>
    );
    let r!: ReturnType<typeof create>;
    act(() => {
      r = create(live, { createNodeMock: nodeMock });
    });
    mounted.push(r);
    expect(seen).toHaveLength(1);
    expect(byClass(r.toJSON() as Json | Json[], "run-trigger")).toHaveLength(0);

    act(() => {
      r.update(
        <CardPolicyProvider value={policy}>
          <SegmentView segments={segs} />
        </CardPolicyProvider>,
      );
    });
    // The run has folded behind its trigger…
    expect(byClass(r.toJSON() as Json | Json[], "run-trigger")).toHaveLength(1);
    expect(chips(r.toJSON() as Json | Json[])).toHaveLength(0);
    // …and the prose was never mounted a second time.
    expect(seen).toHaveLength(1);

    // The control: a genuinely fresh tree DOES count a mount, so the assertion
    // above is a fact about reconciliation and not about the counter.
    act(() => {
      mounted.push(create(live, { createNodeMock: nodeMock }));
    });
    expect(seen).toHaveLength(2);
  });
});

describe("which stretches get a trigger", () => {
  test("a SINGLE settled chip gets one — there is no minimum", () => {
    const json = view([text("one call:"), tool("a", "Read")]).toJSON() as Json | Json[];
    expect(byClass(json, "run-trigger")).toHaveLength(1);
    expect(chips(json)).toHaveLength(0);
  });

  test("thinking, notice and tool fold into ONE run, and open as themselves", () => {
    const policy = createCardPolicy();
    const segs = [text("lead"), think("why"), notice("shell finished"), tool("a", "Read")];
    const shut = view(segs, {}, policy).toJSON() as Json | Json[];
    expect(byClass(shut, "run-trigger")).toHaveLength(1);
    expect(byClass(shut, "thinking")).toHaveLength(0);
    expect(byClass(shut, "seg-notice")).toHaveLength(0);
    expect(chips(shut)).toHaveLength(0);
    // One trigger for three members of three different kinds — the run is
    // "these steps happened together", not "these tool calls did".
    expect(words(byClass(shut, "run-trigger")[0]!)).toBe("more▸");
  });

  test("a live turn's trailing stretch renders member by member, with no trigger", () => {
    const segs = [text("hi"), tool("a", "Read"), tool("b", "Bash")];
    const json = view(segs, { live: true }).toJSON() as Json | Json[];
    expect(byClass(json, "run-trigger")).toHaveLength(0);
    // The prose keeps its own block (review #4) — what it does not have is a
    // trigger in it.
    expect(byClass(json, "has-trigger")).toHaveLength(0);
    expect(chips(json)).toHaveLength(2);
    // A running call does the same to a settled turn's stretch.
    const running = view([text("hi"), tool("c", "Read"), tool("d", "Bash", "running")]).toJSON() as
      | Json
      | Json[];
    expect(byClass(running, "run-trigger")).toHaveLength(0);
    expect(chips(running)).toHaveLength(2);
  });

  test("a filed card breaks the run and keeps its own chip individual", () => {
    const segs = [
      tool("a", "Read"),
      tool("b", "Read"),
      tool("c", "Bash"),
      tool("d", "Grep"),
      tool("e", "Glob"),
    ];
    const cardsAfter = new Map<number, React.ReactNode>([
      [2, <div key="card" className="filed-card" />],
    ]);
    const json = view(segs, { cardsAfter }).toJSON() as Json | Json[];
    expect(byClass(json, "run-trigger")).toHaveLength(2);
    // The carded chip is on screen, unfolded, with its card under it.
    expect(chips(json)).toHaveLength(1);
    expect(byClass(json, "filed-card")).toHaveLength(1);
  });
});

describe("opening a run", () => {
  test("the click drops the follow before it changes the log's height (review #3)", () => {
    // Opening a run makes `.chat-log` taller, the scrollport answers a resize by
    // writing `scrollTop = scrollHeight`, and the run the reader just opened
    // went off the bottom of the screen. The toggle tells the port first —
    // through the policy, because a chip is five levels down a memoized tree.
    const policy = createCardPolicy();
    let held = 0;
    policy.holdTail = () => held++;
    const json = view([text("lead"), tool("a", "Read")], {}, policy).toJSON() as Json | Json[];
    const press = (byClass(json, "run-trigger")[0]!.props as { onClick: () => void }).onClick;
    act(() => press());
    expect(held).toBe(1);
    expect(policy.overrides.get("run:tool:a")).toBe(true);
  });

  test("a MEMBER opened inside the run drops the follow too (bugbot)", () => {
    // The run's own trigger held the tail; the chips and thinking blocks under
    // it went through `useCardOpen`, which did not — so during a live turn,
    // where members render individually, opening one grew `.chat-log` and the
    // ResizeObserver scrolled the body that was just opened off screen.
    const policy = createCardPolicy();
    policy.overrides.set("run:tool:a", true);
    let held = 0;
    policy.holdTail = () => held++;
    const json = view([text("lead"), tool("a", "Read")], {}, policy).toJSON() as Json | Json[];
    // The chip's summary is a Radix trigger, so its handler reads the event.
    const press = (byClass(json, "chip-summary")[0]!.props as { onClick: (e: unknown) => void })
      .onClick;
    act(() => press({ nativeEvent: {}, defaultPrevented: false, preventDefault() {} }));
    expect(held).toBe(1);
    expect(policy.overrides.get("tool:a")).toBe(true);
  });


  test("the members render exactly as they do outside one, under their own keys", () => {
    const policy = createCardPolicy();
    // `run:` over the first member's key — never the member's own key, or
    // opening the run would open its first chip too.
    policy.overrides.set("run:tool:a", true);
    const segs = [text("lead"), tool("a", "Read"), think("why"), notice("done"), tool("b", "Bash")];
    const json = view(segs, {}, policy).toJSON() as Json | Json[];
    expect(words(byClass(json, "run-trigger")[0]!)).toBe("less▾");
    expect(chips(json)).toHaveLength(2);
    expect(byClass(json, "thinking")).toHaveLength(1);
    expect(byClass(json, "seg-notice")).toHaveLength(1);
    // Each member is still folded under the key it had before the run existed,
    // so a chip the reader opens stays open.
    expect(byClass(json, "chip-body")).toHaveLength(0);
    policy.overrides.set("tool:b", true);
    const again = view(segs, {}, policy).toJSON() as Json | Json[];
    expect(byClass(again, "chip-body")).toHaveLength(1);
  });
});
