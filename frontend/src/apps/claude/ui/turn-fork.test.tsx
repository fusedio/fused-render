// The fork button: drawn only for a sent, transcript-backed user turn when the
// host passes `onFork`, and never inside the bubble (the re-attach probe reads
// `.user .bubble`).
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";

const { Turn } = await import("./Turn");

import type { UserTurn } from "../protocol/controller-api";

const html = (turn: UserTurn, onFork?: (k: string) => void) =>
  renderToStaticMarkup(createElement(Turn, { turn, ...(onFork ? { onFork } : {}) }));

const base: UserTurn = { role: "user", key: "k1", text: "hello", uuid: "u1" };

describe("Turn fork button", () => {
  test("drawn with uuid + onFork, before the bubble", () => {
    const out = html(base, () => {});
    expect(out).toContain('class="turn-fork"');
    expect(out).toContain('aria-label="Fork from here"');
    expect(out).toContain('data-hint="Fork from here"');
    expect(out.indexOf("turn-fork")).toBeLessThan(out.indexOf('class="bubble'));
    const bubble = out.slice(out.indexOf('class="bubble'));
    expect(bubble.slice(0, bubble.indexOf("</div>"))).not.toContain("turn-fork");
  });
  test("absent without onFork or without uuid", () => {
    expect(html(base)).not.toContain("turn-fork");
    const { uuid: _u, ...noUuid } = base;
    expect(html(noUuid as UserTurn, () => {})).not.toContain("turn-fork");
  });
});
