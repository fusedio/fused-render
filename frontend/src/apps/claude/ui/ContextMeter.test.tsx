// The meter's own contract: when it draws at all, what it says, and that the
// sentence the pointer reads and the sentence a screen reader hears are one
// string rather than two that can drift.
import { expect, test } from "bun:test";
import { create, type ReactTestRenderer } from "react-test-renderer";
import { act } from "react";

import { ContextMeter } from "./ContextMeter";

function render(node: React.ReactElement): ReactTestRenderer {
  let out: ReactTestRenderer | undefined;
  act(() => {
    out = create(node);
  });
  return out!;
}

test("nothing at all until a reply has spent something", () => {
  // A chat whose first reply has not landed draws NO seat — an empty meter in
  // an empty conversation is chrome that says nothing, and a reader learns to
  // ignore the seat before it ever has news.
  expect(render(<ContextMeter tokens={0} window={200_000} />).toJSON()).toBeNull();
  expect(render(<ContextMeter tokens={-1} window={200_000} />).toJSON()).toBeNull();
  expect(render(<ContextMeter tokens={NaN} window={200_000} />).toJSON()).toBeNull();
});

test("the percentage, and ONE sentence for the caption and the label", () => {
  const meter = render(<ContextMeter tokens={84_000} window={200_000} />).root
    .findByProps({ role: "img" });
  const sentence = "Context 84k of 200k · 116k left";
  // `data-hint` is this app's tooltip (platform/lib/hints), never `title`.
  expect(meter.props["data-hint"]).toBe(sentence);
  expect(meter.props["aria-label"]).toBe(sentence);
  expect(meter.props.className).toBe("c-ctxmeter is-ok");
  const pct = meter.findByProps({ className: "c-ctxmeter-pct" });
  expect(pct.props.children).toEqual([42, "%"]);
  // The drawing is hidden from the reader the label already told: announcing
  // the ring as well would say the same thing twice.
  expect(String(pct.props["aria-hidden"])).toBe("true");
});

test("quiet until 70%, then warning, then the error colour", () => {
  const level = (tokens: number) =>
    render(<ContextMeter tokens={tokens} window={200_000} />).root.findByProps({
      role: "img",
    }).props.className;
  expect(level(100_000)).toContain("is-ok");
  expect(level(140_000)).toContain("is-warn");
  expect(level(190_000)).toContain("is-full");
});

test("compact keeps the ring and drops the digits", () => {
  const meter = render(<ContextMeter tokens={84_000} window={200_000} compact />).root
    .findByProps({ role: "img" });
  expect(meter.props.className).toContain("is-compact");
  expect(meter.findAllByProps({ className: "c-ctxmeter-pct" })).toHaveLength(0);
  // The ring survives: at the tightest rungs the meter costs a glyph rather
  // than a seat, and it still says roughly how full the window is.
  expect(meter.findAllByProps({ className: "c-ctxmeter-ring" }).length).toBeGreaterThan(0);
});

test("a `[1m]` window reports its own scale, not 200k's", () => {
  // Same token count, a different window: the sentence and the fill both have
  // to follow the model the reply was actually made with.
  const meter = render(<ContextMeter tokens={200_000} window={1_000_000} />).root
    .findByProps({ role: "img" });
  expect(meter.props["aria-label"]).toBe("Context 200k of 1M · 800k left");
  expect(meter.findByProps({ className: "c-ctxmeter-pct" }).props.children).toEqual([20, "%"]);
});
