// The meter's own contract: when it draws at all, what it says, and that the
// sentence the pointer reads and the sentence a screen reader hears are one
// string rather than two that can drift.
//
// The popover's CONTENTS are tested through `ContextReportView` directly rather
// than by opening the Popover: the open surface is portaled into a document
// this renderer does not have, and what is worth pinning is the block chart and
// the legend, not Base UI's own dismissal contract (which `PillSelect` already
// relies on everywhere else in this row).
import { expect, test } from "bun:test";
import { create, type ReactTestRenderer } from "react-test-renderer";
import { act } from "react";

import { ContextMeter, ContextReportView } from "./ContextMeter";
import type { ContextUsage } from "../protocol/types";

function render(node: React.ReactElement): ReactTestRenderer {
  let out: ReactTestRenderer | undefined;
  act(() => {
    out = create(node);
  });
  return out!;
}

function usage(over: Partial<ContextUsage> = {}): ContextUsage {
  return {
    input_tokens: 0,
    cache_creation_input_tokens: 0,
    cache_read_input_tokens: 0,
    output_tokens: 0,
    model: "",
    compacted: false,
    ...over,
  };
}

test("nothing at all until a reply has spent something", () => {
  // A chat whose first reply has not landed draws NO seat — and neither does
  // the moment right after a compaction, where the CLI's own statusline
  // reports nothing either. An empty meter in an empty conversation is chrome
  // that says nothing, and a reader learns to ignore the seat before it ever
  // has news.
  expect(render(<ContextMeter usage={null} model="sonnet" />).toJSON()).toBeNull();
  expect(
    render(<ContextMeter usage={usage({ output_tokens: 40 })} model="sonnet" />).toJSON(),
  ).toBeNull();
});

test("the ring carries the digits, and ONE sentence answers both readers", () => {
  const meter = render(
    <ContextMeter
      usage={usage({ cache_read_input_tokens: 84_000, model: "claude-haiku-4-5" })}
      model="claude-haiku-4-5"
    />,
  ).root.findByType("button");
  const sentence = "Context: 84k of 200k tokens (42%)";
  // `data-hint` is this app's tooltip (platform/lib/hints), never `title`.
  expect(meter.props["data-hint"]).toBe(sentence);
  expect(meter.props["aria-label"]).toBe(sentence);
  expect(meter.props.className).toBe("c-ctxmeter");
  const pct = meter.findByProps({ className: "c-ctxmeter-pct" });
  // The number lives INSIDE the ring, and with no "%" beside it: the sentence
  // above spells the unit out, and two glyphs is what fits in the ring.
  expect(pct.props.children).toBe(42);
  expect(String(pct.props["aria-hidden"])).toBe("true");
  // The drawing is hidden from the reader the label already told.
  const ring = meter.findByProps({ className: "c-ctxmeter-ring" });
  expect(String(ring.props["aria-hidden"])).toBe("true");
});

test("the arc is the percentage, drawn from twelve o'clock", () => {
  const arc = (model: string, tokens: number) =>
    render(
      <ContextMeter usage={usage({ cache_read_input_tokens: tokens, model })} model={model} />,
    ).root.findByProps({ className: "c-ctxmeter-arc" }).props;
  const circumference = 2 * Math.PI * 12;
  expect(arc("claude-haiku-4-5", 100_000).strokeDasharray).toBe(
    `${(circumference / 2).toFixed(2)} ${circumference.toFixed(2)}`,
  );
  // Rotated a quarter turn back, so the arc starts at the top like every other
  // dial a reader has seen.
  expect(arc("claude-haiku-4-5", 100_000).transform).toBe("rotate(-90 14 14)");
  // …about the TRACK's centre, which the arc shares — an arc centred elsewhere
  // and rotated about the track is a crescent, not a fill (Bugbot, PR #1253).
  const ring = create(
    <ContextMeter usage={usage({ cache_read_input_tokens: 1000, model: "claude-haiku-4-5" })} model="claude-haiku-4-5" />,
  ).root;
  const track = ring.findByProps({ className: "c-ctxmeter-track" }).props;
  const filled = ring.findByProps({ className: "c-ctxmeter-arc" }).props;
  expect([filled.cx, filled.cy]).toEqual([track.cx, track.cy]);
  expect(filled.transform).toBe(`rotate(-90 ${track.cx} ${track.cy})`);
  // A million-token model reports its own scale: the same count, a quieter ring.
  expect(arc("claude-sonnet-5", 100_000).strokeDasharray).toBe(
    `${(circumference * 0.1).toFixed(2)} ${circumference.toFixed(2)}`,
  );
});

test("a compacted reading says it is an estimate", () => {
  const meter = render(
    <ContextMeter
      usage={usage({ input_tokens: 9_876, model: "claude-sonnet-5", compacted: true })}
      model="claude-sonnet-5"
    />,
  ).root.findByType("button");
  expect(meter.props["aria-label"]).toBe(
    "Context: 9.8k of 1M tokens (1%) · compacted, estimate",
  );
});

test("the breakdown is `/context`: a block chart, then its legend", () => {
  const view = render(
    <ContextReportView
      usage={usage({ cache_read_input_tokens: 84_000, model: "claude-haiku-4-5" })}
      model="claude-haiku-4-5"
    />,
  ).root;
  expect(view.findByProps({ className: "c-ctxpop-title" }).props.children).toBe(
    "Context Usage",
  );
  // 100 squares on a 200k window, 42 of them in use.
  const squares = view.findAllByProps({ className: "c-ctxpop-grid" })[0]!.props
    .children as unknown[];
  expect(squares).toHaveLength(100);
  const rows = view.findAllByProps({ className: "c-ctxpop-row" });
  expect(rows).toHaveLength(2);
  expect(
    rows[0]!.findByProps({ className: "c-ctxpop-label" }).props.children,
  ).toBe("Messages:");
  expect(
    rows[1]!.findByProps({ className: "c-ctxpop-label" }).props.children,
  ).toBe("Free space:");
  // The model id is printed too — the window's size follows it, so a reader
  // wondering why the meter reads 42% can see which head it is 42% of.
  expect(
    view.findAllByProps({ className: "c-ctxpop-dim" })[0]!.props.children,
  ).toBe("claude-haiku-4-5");
});

test("an enforced window draws its autocompact buffer, and 200 squares", () => {
  const view = render(
    <ContextReportView
      usage={usage({ cache_read_input_tokens: 100_000, model: "claude-opus-5" })}
      model="claude-opus-5"
    />,
  ).root;
  const squares = view.findAllByProps({ className: "c-ctxpop-grid" })[0]!.props
    .children as unknown[];
  expect(squares).toHaveLength(200);
  expect(
    view
      .findAllByProps({ className: "c-ctxpop-row" })
      .map((row) => row.findByProps({ className: "c-ctxpop-label" }).props.children),
  ).toEqual(["Messages:", "Autocompact buffer:", "Free space:"]);
});
