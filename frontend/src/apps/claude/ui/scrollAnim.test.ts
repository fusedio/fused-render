import { afterEach, beforeEach, describe, expect, test } from "bun:test";

import { animateScroll, JUMP_MS } from "./scrollAnim";

// A fake port and a hand-cranked clock + frame queue.
let t = 0;
let frames: Array<() => void> = [];
const g = globalThis as Record<string, unknown>;
const saved: Record<string, unknown> = {};
beforeEach(() => {
  t = 0;
  frames = [];
  for (const k of ["requestAnimationFrame", "cancelAnimationFrame", "performance", "matchMedia"]) saved[k] = g[k];
  g.requestAnimationFrame = (fn: () => void) => frames.push(fn);
  g.cancelAnimationFrame = () => {};
  g.performance = { now: () => t };
  g.matchMedia = () => ({ matches: false });
});
afterEach(() => {
  for (const k of Object.keys(saved)) g[k] = saved[k];
});
const tick = (ms: number) => {
  t += ms;
  const run = frames;
  frames = [];
  for (const f of run) f();
};
function port() {
  const listeners = new Map<string, () => void>();
  return {
    scrollTop: 0,
    addEventListener: (k: string, fn: () => void) => listeners.set(k, fn),
    removeEventListener: (k: string) => listeners.delete(k),
    fire: (k: string) => listeners.get(k)?.(),
  };
}

describe("the jump scroll", () => {
  test(`eases in and out to the target inside ${JUMP_MS}ms and lands exactly`, () => {
    const el = port();
    let landed = null as boolean | null;
    animateScroll(el as unknown as HTMLElement, () => 1000, (l) => (landed = l));
    tick(16);
    const first = el.scrollTop;
    expect(first).toBeGreaterThan(0);
    expect(first).toBeLessThan(50); // a soft start
    tick(34);
    expect(el.scrollTop).toBeCloseTo(500, -1); // half way at half time
    tick(16);
    tick(50);
    expect(el.scrollTop).toBe(1000);
    expect(landed).toBe(true);
  });
  test("follows a target that moves (a log still growing)", () => {
    const el = port();
    let end = 1000;
    animateScroll(el as unknown as HTMLElement, () => end);
    tick(50);
    end = 1500;
    tick(60);
    expect(el.scrollTop).toBe(1500);
  });
  test("a wheel tick cancels it where it is", () => {
    const el = port();
    let landed = null as boolean | null;
    animateScroll(el as unknown as HTMLElement, () => 1000, (l) => (landed = l));
    tick(30);
    const at = el.scrollTop;
    el.fire("wheel");
    tick(100);
    expect(el.scrollTop).toBe(at);
    expect(landed).toBe(false);
  });
  test("reduced motion: instant", () => {
    g.matchMedia = () => ({ matches: true });
    const el = port();
    animateScroll(el as unknown as HTMLElement, () => 700);
    expect(el.scrollTop).toBe(700);
  });
});
