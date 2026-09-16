// The `show more` trigger and the run it opens (design.md §A) — where the word
// sits, which stretches get one, and what comes out when it is clicked.
//
// `react-test-renderer`, the `chip-copy.test.tsx` pattern: no real DOM, so a
// run's open state is read off the collapse policy rather than off a rendered
// panel.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestRendererJSON } from "react-test-renderer";

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { CardPolicyProvider, createCardPolicy, type CardPolicy } from "./cardPolicy";
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

/** The SAME transcript, re-rendered — never a second `view()`. A trigger is
 *  keyed by its SEAT (review #6) and a seat by its position inside a NUMBERED
 *  container (`cardKey`); a fresh mount takes a fresh number, so only an update
 *  is the same reader looking at the same reply. */
const again = (
  r: ReturnType<typeof create>,
  segs: Segment[],
  props: Record<string, unknown>,
  policy: CardPolicy,
): Json | Json[] => {
  act(() =>
    r.update(
      <CardPolicyProvider value={policy}>
        <SegmentView segments={segs} {...props} />
      </CardPolicyProvider>,
    ),
  );
  return r.toJSON() as Json | Json[];
};

/** Open a run the way a reader does. Nothing outside `SegmentView` can spell
 *  the key any more, which is the point of review #6. */
const press = (json: Json | Json[] | null, cls = "run-trigger") =>
  act(() => (byClass(json, cls)[0]!.props as { onClick: () => void }).onClick());

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
    expect(words(byClass(blocks[0]!, "run-trigger")[0]!)).toBe("show more");
    expect((blocks[0]!.props as { className: string }).className).not.toContain("is-bare");
    // Collapsed: not one chip is mounted.
    expect(chips(json)).toHaveLength(0);
  });

  test("A TURN THAT OPENS ON TOOL CALLS SEATS THE WORD ON THE PROSE THAT FOLLOWS (Akshil 2026-09-15)", () => {
    // Q1 revised: the leading run used to take a bare right-aligned line ABOVE
    // the first paragraph — the machinery row §A exists to delete, in a smaller
    // hat. It attaches FORWARD instead, same corner seat as a run after prose,
    // and its members open ABOVE that paragraph (chronological).
    const policy = createCardPolicy();
    const segs = [tool("a", "Read"), text("Done."), tool("b", "Bash")];
    const r = view(segs, {}, policy);
    const shut = r.toJSON() as Json | Json[];
    expect(byClass(shut, "is-bare")).toHaveLength(0);
    // ONE word for BOTH runs (before and after the paragraph), in the prose's
    // own block — never two triggers stacked in one corner.
    const blocks = byClass(shut, "seg-block");
    expect(blocks).toHaveLength(1);
    expect(proses(blocks[0]!)).toEqual(["Done."]);
    const trigger = byClass(shut, "run-trigger");
    expect(trigger).toHaveLength(1);
    expect(words(trigger[0]!)).toBe("show more");
    expect((blocks[0]!.props as { className: string }).className).toContain("has-trigger");

    // One click opens both runs, each at its own chronological position: the
    // leading chip above the paragraph, the trailing one below it.
    press(shut);
    const open = again(r, segs, {}, policy);
    expect(chips(open)).toHaveLength(2);
    const order = (Array.isArray(open) ? open : [open]).map((n) =>
      byClass(n, "toolchip").length ? "chip" : byClass(n, "seg-block").length ? "prose" : "?",
    );
    expect(order).toEqual(["chip", "prose", "chip"]);
    expect(byClass(open, "run-trigger")).toHaveLength(1);
  });

  test("a turn with NO prose at all keeps the bare own-line trigger", () => {
    const json = view([tool("a", "Read"), tool("b", "Bash")]).toJSON() as Json | Json[];
    const block = byClass(json, "seg-block")[0]!;
    expect((block.props as { className: string }).className).toContain("is-bare");
    expect(proses(block)).toEqual([]);
    expect(byClass(json, "run-trigger")).toHaveLength(1);
  });

  test("a run BEHIND the growing tail still takes a bare line", () => {
    // Looking backward the tail is not a seat: a word in the corner of a
    // paragraph that is still being written rides its last line down the
    // screen. (Reachable only for a turn whose tail is followed by a closed
    // stretch — the tail is prose, so it is never a run.)
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

  test("A LEADING RUN SEATS ON THE TAIL WHILE IT IS STILL STREAMING (review #5)", () => {
    // The tail used to be refused as a seat outright, so a turn that opened on
    // tool calls showed its word on a bare line above the answer for the whole
    // of the answer — and then teleported it into the corner the moment the
    // turn settled. The block around the tail is the same keyed element either
    // way (`segBlock`); only its prose slot is rewritten per frame.
    const segs = [tool("a", "Read"), text("Done so f")];
    const json = view(segs, { tail: { index: 1, text: "Done so f", cursor: true } }).toJSON() as
      | Json
      | Json[];
    expect(byClass(json, "is-bare")).toHaveLength(0);
    const blocks = byClass(json, "seg-block");
    expect(blocks).toHaveLength(1);
    expect((blocks[0]!.props as { className: string }).className).toContain("has-trigger");
    // AND THE CARET IS STILL THERE: the trigger and the caret share slot 1, so
    // the word cannot be won at the cost of the thing that says it is typing.
    expect(byClass(blocks[0]!, "run-trigger")).toHaveLength(1);
    expect(byClass(blocks[0]!, "cursor")).toHaveLength(1);
  });

  test("THE TRIGGER'S KEY IS THE SEAT'S, so a split run keeps the reader's word open (review #6)", () => {
    // Keyed off the leading run's first chip, the pair's identity changed the
    // moment a filed card split that run in two — chip `a` left on a bare line,
    // chip `b` inheriting the seat — and the run the reader had opened shut
    // itself under them. The seat cannot change: it is the paragraph.
    const policy = createCardPolicy();
    const segs = [tool("a", "Read"), tool("b", "Bash"), text("Done.")];
    const r = view(segs, {}, policy);
    press(r.toJSON() as Json | Json[]);
    const open = r.toJSON() as Json | Json[];
    expect(words(byClass(open, "run-trigger")[0]!)).toBe("show less");
    expect(chips(open)).toHaveLength(2);

    // The card lands and splits the stretch: `a` is now its own bare run, `b`
    // holds the seat. Same key, so the paragraph's word is still open.
    const cardsAfter = new Map<number, React.ReactNode>([
      [0, <div key="card" className="filed-card" />],
    ]);
    const split = again(r, segs, { cardsAfter }, policy);
    const seated = byClass(split, "run-trigger").filter(
      (t) => !byClass(split, "is-bare").some((b) => byClass(b, "run-trigger").includes(t)),
    );
    expect(seated).toHaveLength(1);
    expect(words(seated[0]!)).toBe("show less");
    expect(byClass(split, "filed-card")).toHaveLength(1);
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
    expect(words(byClass(shut, "run-trigger")[0]!)).toBe("show more");
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
    press(json);
    expect(held).toBe(1);
    // THE SEAT'S KEY (review #6): the paragraph the word is drawn in, not the
    // first chip behind it — the run that is "first" changes when a card splits
    // the stretch or a live turn suppresses half of it, and the reader's open
    // word must not change with it.
    const opened = [...policy.overrides];
    expect(opened).toHaveLength(1);
    expect(opened[0]![0]).toMatch(/^run:seat:\d+:0$/);
    expect(opened[0]![1]).toBe(true);
  });

  test("a MEMBER opened inside the run drops the follow too (bugbot)", () => {
    // The run's own trigger held the tail; the chips and thinking blocks under
    // it went through `useCardOpen`, which did not — so during a live turn,
    // where members render individually, opening one grew `.chat-log` and the
    // ResizeObserver scrolled the body that was just opened off screen.
    const policy = createCardPolicy();
    const segs = [text("lead"), tool("a", "Read")];
    const r = view(segs, {}, policy);
    press(r.toJSON() as Json | Json[]);
    let held = 0;
    policy.holdTail = () => held++;
    const json = again(r, segs, {}, policy);
    // The chip's summary is a Radix trigger, so its handler reads the event.
    const open = (byClass(json, "chip-summary")[0]!.props as { onClick: (e: unknown) => void })
      .onClick;
    act(() => open({ nativeEvent: {}, defaultPrevented: false, preventDefault() {} }));
    expect(held).toBe(1);
    expect(policy.overrides.get("tool:a")).toBe(true);
  });


  test("the members render exactly as they do outside one, under their own keys", () => {
    const policy = createCardPolicy();
    const segs = [text("lead"), tool("a", "Read"), think("why"), notice("done"), tool("b", "Bash")];
    // `run:seat:` over the SEAT's key — never a member's own key, or opening
    // the run would open its first chip too.
    const r = view(segs, {}, policy);
    press(r.toJSON() as Json | Json[]);
    const json = again(r, segs, {}, policy);
    expect(words(byClass(json, "run-trigger")[0]!)).toBe("show less");
    expect(chips(json)).toHaveLength(2);
    expect(byClass(json, "thinking")).toHaveLength(1);
    expect(byClass(json, "seg-notice")).toHaveLength(1);
    // Each member is still folded under the key it had before the run existed,
    // so a chip the reader opens stays open.
    expect(byClass(json, "chip-body")).toHaveLength(0);
    const chipB = (byClass(json, "chip-summary")[1]!.props as { onClick: (e: unknown) => void })
      .onClick;
    act(() => chipB({ nativeEvent: {}, defaultPrevented: false, preventDefault() {} }));
    expect(policy.overrides.get("tool:b")).toBe(true);
    expect(byClass(r.toJSON() as Json | Json[], "chip-body")).toHaveLength(1);
  });
});

// ── WHERE THE WORD SITS, ACROSS TURNS (Akshil, 2026-09-15) ───────────────────
//
// `right: 0` only means "the right edge of the transcript" if the positioned
// box reaches that edge. `.turn.assistant` is a flex row and `.body` had no
// `flex`, so it shrink-wrapped to its content: a turn ending in a short line
// put its `show more` hundreds of px in from the column while the next turn's sat
// at the margin. A corner affordance at a different x per row is not a corner.
//
// Geometry, so it is the SHEET that is asserted — this renderer lays nothing
// out. The rules are read whole rather than grepped for a token, so a
// `flex: 1` in some neighbouring block cannot answer for this one.
describe("the trigger's column", () => {
  const sheet = readFileSync(join(import.meta.dir, "../styles/transcript.css"), "utf8");
  const ruleFor = (selector: string): string => {
    const at = sheet.indexOf(selector + " {");
    expect(at).toBeGreaterThan(-1);
    const open = sheet.slice(at + selector.length + 2);
    return open.slice(0, open.indexOf("}"));
  };

  test("the reply's body takes the whole column, not the width of its words", () => {
    const body = ruleFor(".chat-root .turn.assistant .body");
    expect(body).toContain("flex: 1");
    // …and it still shrinks for a long unbroken token rather than widening the
    // row — the half that was already right.
    expect(body).toContain("min-width: 0");
  });

  test("every block in it is full width, and the word trails the last sentence INLINE", () => {
    const block = ruleFor(".chat-root .seg-block");
    expect(block).toContain("width: 100%");
    expect(block).toContain("display: block");
    // INLINE, AT THE END OF THE LAST SENTENCE (Akshil, 2026-09-16): the prose
    // box gives up its box so the trigger flows in the same line as the last
    // paragraph, which is made inline for it. No grid, no positioning.
    expect(ruleFor(".chat-root .seg-block.has-trigger > .seg-text")).toContain("display: contents");
    expect(ruleFor(".chat-root .seg-block.has-trigger > .seg-text > p:last-child")).toContain(
      "display: inline",
    );
    expect(sheet).not.toContain("grid-row: 1");
    expect(sheet).not.toContain("--c-run-trigger-w");
  });

  test("the word is set in the PROSE's type, italic", () => {
    const trigger = ruleFor(".chat-root .run-trigger");
    expect(trigger).toContain("font: inherit");
    expect(trigger).toContain("line-height: inherit");
    expect(trigger).toContain("font-style: italic");
    expect(trigger).not.toContain("font-size:");
  });

  test("OPEN, the word wears a pill; shut, it is the bare word (Akshil 2026-09-16)", () => {
    const open = ruleFor('.chat-root .run-trigger[aria-expanded="true"]');
    expect(open).toContain("border: 1px solid var(--c-border)");
    expect(open).toContain("border-radius: 999px");
    expect(ruleFor(".chat-root .run-trigger")).toContain("border: 0");
  });

  test("THE WORDS ARE THE WHOLE CONTROL — no chevron (Akshil 2026-09-15)", () => {
    // The state is already in the words (`more` vs `less`), so the glyph said it
    // twice — and it was the one part of the trigger that is not prose.
    const r = view([text("Here goes."), tool("a", "Read"), tool("b", "Bash")]);
    const draw = () => r.toJSON() as Json | Json[];
    const shut = byClass(draw(), "run-trigger")[0]!;
    expect(words(shut)).toBe("show more");
    expect(byClass(shut, "run-chev")).toHaveLength(0);
    press(draw());
    const open = byClass(draw(), "run-trigger")[0]!;
    expect(words(open)).toBe("show less");
    expect(byClass(open, "run-chev")).toHaveLength(0);
    // …and the stylesheet has nothing left to style.
    expect(sheet).not.toContain("run-chev");
  });

  test("the BARE case lands on the same edge, by the same box", () => {
    // A run with no prose in front of it takes its own line — `text-align:
    // right` inside a block that now reaches the column, so the word ends up
    // exactly where the positioned one does.
    expect(ruleFor(".chat-root .seg-block.is-bare")).toContain("text-align: right");
  });
});
