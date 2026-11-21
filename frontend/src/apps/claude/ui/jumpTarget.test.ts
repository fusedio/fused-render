import { describe, expect, test } from "bun:test";

import { downShown, sideOf, upShown } from "./jumpTarget";

const port = (scrollTop: number) => ({ clientHeight: 500, scrollHeight: 3000, scrollTop });

describe("two buttons, one direction each", () => {
  test("↑ once your message's top has scrolled off, not before", () => {
    expect(upShown(-300)).toBe(true);
    expect(upShown(-10)).toBe(false);
    expect(upShown(40)).toBe(false);
    expect(upShown(900)).toBe(false);
    expect(upShown(null)).toBe(false);
  });
  test("↓ away from the end, not at it", () => {
    expect(downShown(port(0))).toBe(true);
    expect(downShown(port(1500))).toBe(true);
    expect(downShown(port(2450))).toBe(false);
    expect(downShown(port(2500))).toBe(false);
  });
});

describe("the pill goes on the side its target is", () => {
  test("above, below, or on screen", () => {
    expect(sideOf(-200, -150, 500)).toBe("above");
    expect(sideOf(700, 750, 500)).toBe("below");
    expect(sideOf(100, 130, 500)).toBe("on");
    expect(sideOf(-200, 40, 500)).toBe("on");
  });
});
