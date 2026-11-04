// The per-message Breakout button: a SIBLING before the bubble, like the fork
// button (the re-attach probe matches `.user .bubble`'s text).
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";
import { act, create } from "react-test-renderer";

const { Turn } = await import("./Turn");


import type { AssistantTurn, UserTurn } from "../protocol/controller-api";

type OnBreakout = (key: string, opts: { add: boolean }) => void;
const html = (turn: UserTurn | AssistantTurn, onBreakout?: OnBreakout, selected = false) =>
  renderToStaticMarkup(
    createElement(Turn, { turn, ...(onBreakout ? { onBreakout } : {}), ...(selected ? { breakoutSelected: true } : {}) }),
  );

const base: UserTurn = { role: "user", key: "k1", text: "hello", uuid: "u1" };
const answer: AssistantTurn = { role: "assistant", key: "k2", text: "hi there" };

describe("Turn breakout button", () => {
  test("drawn with onBreakout, before the bubble", () => {
    const out = html(base, () => {});
    expect(out).toContain('class="turn-breakout"');
    expect(out).toContain('aria-label="Breakout from here"');
    expect(out).toContain('data-hint="Breakout from here"');
    expect(out.indexOf("turn-breakout")).toBeLessThan(out.indexOf('class="bubble'));
  });
  test("drawn on an assistant turn too, and on a user turn with no uuid", () => {
    expect(html(answer, () => {})).toContain('class="turn-breakout"');
    const { uuid: _u, ...noUuid } = base;
    expect(html(noUuid as UserTurn, () => {})).toContain('class="turn-breakout"');
  });
  test("absent on an assistant turn that is still streaming", () => {
    expect(html({ ...answer, streaming: true } as AssistantTurn, () => {})).not.toContain("turn-breakout");
    expect(html({ ...answer, streaming: false } as AssistantTurn, () => {})).toContain("turn-breakout");
  });
  test("absent without onBreakout", () => {
    expect(html(base)).not.toContain("turn-breakout");
    expect(html(answer)).not.toContain("turn-breakout");
  });
  test("a selected turn wears is-breakout-selected, on either role", () => {
    expect(html(base, () => {}, true)).toContain("is-breakout-selected");
    expect(html(answer, () => {}, true)).toContain("is-breakout-selected");
    expect(html(base, () => {})).not.toContain("is-breakout-selected");
  });
  test("a click passes the turn key and whether shift was held", () => {
    const seen: [string, boolean][] = [];
    let r!: ReturnType<typeof create>;
    act(() => {
      r = create(createElement(Turn, { turn: answer, onBreakout: (k: string, o: { add: boolean }) => void seen.push([k, o.add]) }));
    });
    const btn = r.root.findByProps({ className: "turn-breakout" });
    act(() => btn.props.onClick({ shiftKey: false }));
    act(() => btn.props.onClick({ shiftKey: true }));
    expect(seen).toEqual([["k2", false], ["k2", true]]);
  });
});
