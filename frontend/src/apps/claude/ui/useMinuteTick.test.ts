import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, describe, expect, test } from "bun:test";
import { act, create } from "react-test-renderer";
import { createElement } from "react";

import { MINUTE_TICK_MS, minuteTickListeners, useMinuteTick } from "./useMinuteTick";

const mounted: Array<ReturnType<typeof create>> = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
});

function Probe({ seen }: { seen: number[] }) {
  seen.push(useMinuteTick());
  return null;
}

describe("useMinuteTick", () => {
  test("one interval for every subscriber, started by the first and stopped by the last", () => {
    const calls: number[] = [];
    const realSet = globalThis.setInterval;
    const realClear = globalThis.clearInterval;
    let cleared = 0;
    globalThis.setInterval = ((fn: () => void, ms: number) => {
      calls.push(ms);
      return realSet(fn, 1_000_000);
    }) as typeof setInterval;
    globalThis.clearInterval = ((id: ReturnType<typeof setInterval>) => {
      cleared++;
      realClear(id);
    }) as typeof clearInterval;
    try {
      const a: number[] = [];
      const b: number[] = [];
      let r1!: ReturnType<typeof create>;
      let r2!: ReturnType<typeof create>;
      act(() => {
        r1 = create(createElement(Probe, { seen: a }));
      });
      act(() => {
        r2 = create(createElement(Probe, { seen: b }));
      });
      mounted.push(r1, r2);
      expect(minuteTickListeners()).toBe(2);
      expect(calls).toEqual([MINUTE_TICK_MS]);
      act(() => r1.unmount());
      expect(cleared).toBe(0);
      act(() => r2.unmount());
      expect(cleared).toBe(1);
      expect(minuteTickListeners()).toBe(0);
      mounted.length = 0;
    } finally {
      globalThis.setInterval = realSet;
      globalThis.clearInterval = realClear;
    }
  });
});
