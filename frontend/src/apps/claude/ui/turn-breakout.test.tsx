// The per-message Breakout button: a SIBLING before the bubble, like the fork
// button (the re-attach probe matches `.user .bubble`'s text).
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";

const { Turn } = await import("./Turn");

import type { UserTurn } from "../protocol/controller-api";

const html = (turn: UserTurn, onBreakout?: (k: string) => void) =>
  renderToStaticMarkup(createElement(Turn, { turn, ...(onBreakout ? { onBreakout } : {}) }));

const base: UserTurn = { role: "user", key: "k1", text: "hello", uuid: "u1" };

describe("Turn breakout button", () => {
  test("drawn with uuid + onBreakout, before the bubble", () => {
    const out = html(base, () => {});
    expect(out).toContain('class="turn-breakout"');
    expect(out).toContain('aria-label="Breakout from here"');
    expect(out).toContain('data-hint="Breakout from here"');
    expect(out.indexOf("turn-breakout")).toBeLessThan(out.indexOf('class="bubble'));
  });
  test("absent without onBreakout or without uuid", () => {
    expect(html(base)).not.toContain("turn-breakout");
    const { uuid: _u, ...noUuid } = base;
    expect(html(noUuid as UserTurn, () => {})).not.toContain("turn-breakout");
  });
});
