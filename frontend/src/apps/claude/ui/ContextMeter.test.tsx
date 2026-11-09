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

import { ContextMeter, ContextReportView, useContextBreakdown, type FetchBreakdown } from "./ContextMeter";
import type { ContextBreakdown, ContextUsage } from "../protocol/types";

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

test("a bare ring, ONE sentence for both readers, and a second line that says press", () => {
  const meter = render(
    <ContextMeter
      usage={usage({ cache_read_input_tokens: 84_000, model: "claude-haiku-4-5" })}
      model="claude-haiku-4-5"
    />,
  ).root.findByType("button");
  const sentence = "Context: 84k of 197k tokens before auto-compact (43%)";
  // `data-hint` is this app's tooltip (platform/lib/hints), never `title`. It
  // carries the sentence AND a "Click for details" line: nothing about a plain
  // ring says it opens anything. The spoken name is the sentence alone — a
  // button already tells a screen reader it can be pressed.
  expect(meter.props["data-hint"]).toBe(sentence + "\nClick for details");
  expect(meter.props["aria-label"]).toBe(sentence);
  expect(meter.props.className).toBe("c-ctxmeter");
  // NO digits inside the ring: the tooltip and the popover already say the
  // number, and dropping them is what lets the coin match the 24px pills.
  expect(meter.findAllByProps({ className: "c-ctxmeter-pct" })).toHaveLength(0);
  // The drawing is hidden from the reader the label already told.
  const ring = meter.findByProps({ className: "c-ctxmeter-ring" });
  expect(String(ring.props["aria-hidden"])).toBe("true");
  expect(ring.props.viewBox).toBe("0 0 24 24");
});

test("the ring turns yellow at 75% and red at 90%", () => {
  const cls = (tokens: number) =>
    render(
      <ContextMeter
        usage={usage({ cache_read_input_tokens: tokens, model: "claude-haiku-4-5" })}
        model="claude-haiku-4-5"
      />,
    ).root.findByType("button").props.className as string;
  // Haiku 4.5 compacts at 197k: 71% dim, 76% yellow, 89% yellow, 94% red.
  expect(cls(140_000)).toBe("c-ctxmeter");
  expect(cls(150_000)).toBe("c-ctxmeter is-warn");
  expect(cls(175_000)).toBe("c-ctxmeter is-warn");
  expect(cls(185_000)).toBe("c-ctxmeter is-high");
});

test("the arc is the percentage, drawn from twelve o'clock", () => {
  const arc = (model: string, tokens: number) =>
    render(
      <ContextMeter usage={usage({ cache_read_input_tokens: tokens, model })} model={model} />,
    ).root.findByProps({ className: "c-ctxmeter-arc" }).props;
  const circumference = 2 * Math.PI * 7;
  // 100k of Haiku's 197k compaction point: round(50.8) = 51%.
  expect(arc("claude-haiku-4-5", 100_000).strokeDasharray).toBe(
    `${(circumference * 0.51).toFixed(2)} ${circumference.toFixed(2)}`,
  );
  // Rotated a quarter turn back, so the arc starts at the top like every other
  // dial a reader has seen.
  expect(arc("claude-haiku-4-5", 100_000).transform).toBe("rotate(-90 12 12)");
  // …about the TRACK's centre, which the arc shares — an arc centred elsewhere
  // and rotated about the track is a crescent, not a fill (Bugbot, PR #1253).
  const ring = create(
    <ContextMeter usage={usage({ cache_read_input_tokens: 1000, model: "claude-haiku-4-5" })} model="claude-haiku-4-5" />,
  ).root;
  const track = ring.findByProps({ className: "c-ctxmeter-track" }).props;
  const filled = ring.findByProps({ className: "c-ctxmeter-arc" }).props;
  expect([filled.cx, filled.cy]).toEqual([track.cx, track.cy]);
  expect(filled.transform).toBe(`rotate(-90 ${track.cx} ${track.cy})`);
  // A million-token model reports its own scale (997k): the same count, 10%.
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
    "Context: 9.8k of 997k tokens before auto-compact (1%) · compacted, estimate",
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

test("the popover has Messages and Free space only; a 1M model gets 200 squares", () => {
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
  ).toEqual(["Messages:", "Free space:"]);
});

const BREAKDOWN: ContextBreakdown = {
  categories: [
    { id: "system_prompt", name: "System prompt", tokens: 2484, color: "promptBorder", kind: "used" },
    { id: "mcp_tools", name: "MCP tools", tokens: 900, color: "cyan_FOR_SUBAGENTS_ONLY", kind: "used" },
    { id: "mcp_tools_deferred", name: "MCP tools (deferred)", tokens: 70, color: "inactive", kind: "deferred" },
    { id: "messages", name: "Messages", tokens: 428, color: "purple_FOR_SUBAGENTS_ONLY", kind: "used" },
    { id: "compact_buffer", name: "Compact buffer", tokens: 3000, color: "inactive", kind: "buffer" },
    { id: "free_space", name: "Free space", tokens: 993188, color: "promptBorder", kind: "free" },
  ],
  details: {
    mcp_tools: { items: [{ name: "github", tokens: 900, count: 2 }], more: 3, more_tokens: 120 },
  },
  total_tokens: 3812,
  max_tokens: 1_000_000,
  model: "claude-opus-5",
};

const labels = (view: ReactTestRenderer["root"]) =>
  view
    .findAllByProps({ className: "c-ctxpop-label" })
    .map((label) => label.props.children as string);

test("with the CLI's breakdown: a row per category, deferred dim apart, tones on the squares", () => {
  const view = render(
    <ContextReportView
      usage={usage({ cache_read_input_tokens: 3_812, model: "claude-opus-5" })}
      model="claude-opus-5"
      breakdown={BREAKDOWN}
    />,
  ).root;
  expect(labels(view)).toEqual([
    "System prompt:", "MCP tools:", "Messages:", "Compact buffer:", "Free space:",
    "MCP tools (deferred):",
  ]);
  expect(view.findAllByProps({ className: "c-ctxpop-row is-deferred" })).toHaveLength(1);
  expect(view.findAllByProps({ className: "c-ctxpop-sq is-used is-tone-cyan" })).toHaveLength(1);
  // No "(all context in use)": the categories WERE measured.
  expect(view.findAllByProps({ children: "(all context in use)" })).toHaveLength(0);
});

test("a row with a detail list is a collapsed button that opens an indented list", () => {
  const view = render(
    <ContextReportView
      usage={usage({ cache_read_input_tokens: 3_812, model: "claude-opus-5" })}
      model="claude-opus-5"
      breakdown={BREAKDOWN}
    />,
  ).root;
  const buttons = view.findAllByType("button");
  expect(buttons).toHaveLength(1);
  const toggle = buttons[0]!;
  expect(toggle.props.type).toBe("button");
  expect(toggle.props["aria-expanded"]).toBe(false);
  expect(view.findAllByProps({ className: "c-ctxpop-sub" })).toHaveLength(0);
  act(() => toggle.props.onClick());
  expect(view.findByType("button").props["aria-expanded"]).toBe(true);
  const list = view.findByProps({ className: "c-ctxpop-sub" });
  expect(list.props.id).toBe(view.findByType("button").props["aria-controls"]);
  expect(list.findByProps({ className: "c-ctxpop-subname" }).props.children).toBe("github");
  expect(list.findAllByProps({ children: "(2 tools)" })).toHaveLength(1);
  expect(list.findAllByProps({ children: "+3 more · 120 tokens" })).toHaveLength(1);
  act(() => view.findByType("button").props.onClick());
  expect(view.findAllByProps({ className: "c-ctxpop-sub" })).toHaveLength(0);
});

function Probe({ open, runId, fetch, out, usageKey }: {
  open: boolean;
  runId: string | null;
  fetch: FetchBreakdown;
  out: { value: ContextBreakdown | null };
  usageKey?: number;
}) {
  out.value = useContextBreakdown(open, runId, fetch, usageKey);
  return null;
}

test("the breakdown is asked on open only, and an error falls back to the one-row view", async () => {
  const calls: string[] = [];
  let answer: ContextBreakdown | { error: string } = BREAKDOWN;
  const fetch: FetchBreakdown = (runId) => {
    calls.push(runId);
    return Promise.resolve(answer);
  };
  const out = { value: null as ContextBreakdown | null };
  const r = render(<Probe open={false} runId="run-1" fetch={fetch} out={out} />);
  expect(calls).toEqual([]);
  await act(async () => r.update(<Probe open runId="run-1" fetch={fetch} out={out} />));
  expect(calls).toEqual(["run-1"]);
  expect(out.value).toBe(BREAKDOWN);
  // Closed and reopened: asked again; the error clears it.
  answer = { error: "no live session" };
  await act(async () => r.update(<Probe open={false} runId="run-1" fetch={fetch} out={out} />));
  await act(async () => r.update(<Probe open runId="run-1" fetch={fetch} out={out} />));
  expect(calls).toEqual(["run-1", "run-1"]);
  expect(out.value).toBeNull();
  // No run: nothing is asked.
  await act(async () => r.update(<Probe open runId={null} fetch={fetch} out={out} />));
  expect(calls).toHaveLength(2);
});

test("each new reply total is asked once, a moment later, and an error keeps the last answer", async () => {
  const calls: string[] = [];
  let answer: ContextBreakdown | { error: string } = BREAKDOWN;
  const fetch: FetchBreakdown = (runId) => {
    calls.push(runId);
    return Promise.resolve(answer);
  };
  const out = { value: null as ContextBreakdown | null };
  const wait = () => new Promise((resolve) => setTimeout(resolve, 1600));
  const r = render(<Probe open={false} runId="run-p" fetch={fetch} out={out} usageKey={100} />);
  expect(calls).toEqual([]);
  await act(wait);
  expect(calls).toEqual(["run-p"]);
  expect(out.value).toBe(BREAKDOWN);
  // A second seat on the same reply does not ask again.
  render(<Probe open={false} runId="run-p" fetch={fetch} out={{ value: null }} usageKey={100} />);
  await act(wait);
  expect(calls).toHaveLength(1);
  // The next reply asks again; its error leaves the last answer up.
  answer = { error: "no live session" };
  await act(async () => r.update(<Probe open={false} runId="run-p" fetch={fetch} out={out} usageKey={200} />));
  await act(wait);
  expect(calls).toHaveLength(2);
  expect(out.value).toBe(BREAKDOWN);
});

test("a session target is asked on open only, never prefetched", async () => {
  const calls: string[] = [];
  const fetch: FetchBreakdown = (target) => {
    calls.push(target);
    return Promise.resolve(BREAKDOWN);
  };
  const out = { value: null as ContextBreakdown | null };
  const r = render(<Probe open={false} runId="session:abc" fetch={fetch} out={out} usageKey={100} />);
  await act(() => new Promise((resolve) => setTimeout(resolve, 1600)));
  expect(calls).toEqual([]);
  await act(async () => r.update(<Probe open runId="session:abc" fetch={fetch} out={out} usageKey={100} />));
  expect(calls).toEqual(["session:abc"]);
  expect(out.value).toBe(BREAKDOWN);
});
